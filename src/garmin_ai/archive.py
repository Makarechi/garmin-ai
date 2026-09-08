"""Content-addressed local archive. No raw values are printed or sent remotely."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def has_path_redirect(path: Path) -> bool:
    absolute = path.absolute()
    return any(part.is_symlink() or part.is_junction() for part in (absolute, *absolute.parents))


def private_directory(path: Path) -> Path:
    if has_path_redirect(path):
        raise ValueError(
            "Private directory must not be a symlink or junction or have redirected ancestors"
        )
    durable_directory(path)
    path.chmod(0o700)
    return path


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        flush_windows_volume(path)
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def flush_windows_volume(path: Path) -> None:
    """Flush namespace changes using the documented Windows volume barrier.

    Windows has no documented directory-fsync equivalent. Volume flushing
    requires administrative privileges; failures must never imply durability.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name in ("GetVolumePathNameW", "GetVolumeNameForVolumeMountPointW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        function.restype = wintypes.BOOL
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
    for name in ("FlushFileBuffers", "CloseHandle"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE]
        function.restype = wintypes.BOOL
    mount = ctypes.create_unicode_buffer(32768)
    volume = ctypes.create_unicode_buffer(50)
    if not kernel.GetVolumePathNameW(str(path.absolute()), mount, len(mount)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.GetVolumeNameForVolumeMountPointW(mount.value, volume, len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    # GENERIC_WRITE, share read/write, OPEN_EXISTING; no trailing slash opens
    # the volume itself, rather than its root directory. Never cache the handle.
    handle = create(volume.value.rstrip("\\"), 0x40000000, 3, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.FlushFileBuffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def durable_directory(path: Path) -> Path:
    """Create ancestors in order and persist each entry without changing existing modes."""
    if not path.is_dir():
        durable_directory(path.parent)
        path.mkdir(exist_ok=True, mode=0o700)
    # Also flush existing entries: a prior attempt may have failed after mkdir.
    fsync_directory(path.parent)
    return path


def atomic_private_write(path: Path, data: bytes, *, preserve_parent_mode=False) -> None:
    if preserve_parent_mode:
        if has_path_redirect(path.parent):
            raise ValueError(
                "Destination parent must not be a symlink or junction or have redirected ancestors"
            )
        durable_directory(path.parent)
    else:
        private_directory(path.parent)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


class LocalArchive:
    def __init__(self, root: Path):
        self.root = private_directory(root)

    def put_bytes(self, data: bytes, suffix: str = "bin") -> str:
        if suffix not in {"bin", "json", "fit", "zip"}:
            raise ValueError("Unsupported archive type")
        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest[:2]}/{digest}.{suffix}"
        path = self.root / key
        if not path.exists():
            atomic_private_write(path, data)
        return key

    def put_json(self, payload: object) -> str:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        return self.put_bytes(data, "json")

    def read(self, key: str) -> bytes:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Archive path escapes root")
        return path.read_bytes()
