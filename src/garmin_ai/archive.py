"""Content-addressed local archive. No raw values are printed or sent remotely."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("Private directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_private_write(path: Path, data: bytes, *, preserve_parent_mode=False) -> None:
    if preserve_parent_mode:
        if path.parent.is_symlink():
            raise ValueError("Destination parent must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
