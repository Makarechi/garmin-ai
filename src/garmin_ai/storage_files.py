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


def open_lock_file(path):
    if os.name != "nt":
        return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    return open_windows_lock(path)


def open_windows_lock(path):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    info = kernel.GetFileInformationByHandle
    info.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    info.restype = wintypes.BOOL
    # OPEN_ALWAYS and OPEN_REPARSE_POINT: inspect the entry, never its link target.
    handle = create(str(path), 0xC0000000, 3, None, 4, 0x00200080, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = (wintypes.DWORD * 13)()
        if not info(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if information[0] & 0x400:
            raise ValueError("Lock file must not be a reparse point")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR)
    except BaseException:
        close(handle)
        raise
    return descriptor


@contextmanager
def exclusive_files(settings, *, allow_erased=False):
    directory = settings.lock_dir
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("Lock directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = open_lock_file(directory / "storage.lock")
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
