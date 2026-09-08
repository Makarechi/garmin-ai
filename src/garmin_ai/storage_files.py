"""Cross-process file coordination, including before PostgreSQL is configured."""

import os
from contextlib import contextmanager


def lock_descriptor(descriptor):
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError() from exc
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def exclusive_files(settings, *, allow_erased=False):
    directory = settings.lock_dir
    if directory.is_symlink():
        raise ValueError("Lock directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        directory / "storage.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        try:
            lock_descriptor(descriptor)
        except BlockingIOError as exc:
            raise ValueError("Stop the worker, login or probe before this operation") from exc
        if not allow_erased and any(
            (directory / marker).exists() for marker in ("erased", "activating")
        ):
            raise ValueError("Restore or resume storage after erasure")
        yield
    finally:
        os.close(descriptor)


@contextmanager
def standalone_files(settings, *, allow_erased=False):
    """Database lock also coordinates host commands with Docker Desktop workers."""
    with exclusive_files(settings, allow_erased=allow_erased):
        if not settings.database_url.get_secret_value():
            yield
            return
        from garmin_ai.db import exclusive_ingestion, make_engine

        engine = make_engine(settings)
        try:
            with exclusive_ingestion(engine, allow_erased=allow_erased):
                yield
        finally:
            engine.dispose()
