"""Cross-process file coordination, including before PostgreSQL is configured."""

import fcntl
import os
from contextlib import contextmanager

from garmin_ai.archive import private_directory


@contextmanager
def exclusive_files(settings, *, allow_erased=False):
    directory = private_directory(settings.lock_dir)
    descriptor = os.open(directory / "storage.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Stop the worker, login or probe before this operation") from exc
        if not allow_erased and (directory / "erased").exists():
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
