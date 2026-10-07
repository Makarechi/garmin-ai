"""List preserved absolute storage paths that setup must see in its container."""

import os
import sys
from pathlib import Path

from dotenv import dotenv_values


def preserved_mounts(env_file: Path, bundle_path: Path) -> list[str]:
    if not env_file.exists():
        return []
    values = dotenv_values(env_file)
    current = str(bundle_path)
    result = []
    for key in ("GA_DATA_DIR", "GA_TOKEN_DIR", "GA_BACKUP_DIR", "GA_LOCK_DIR"):
        raw = values.get(key)
        if not raw:
            continue
        path = Path(raw)
        if (
            not path.is_absolute()
            or (key in {"GA_DATA_DIR", "GA_TOKEN_DIR"} and len(path.parts) < 4)
            or any(character in raw for character in ("\n", "\r", ","))
        ):
            raise ValueError(f"{key} must be a dedicated absolute path without commas or newlines")
        normalized = os.path.normpath(raw)
        if os.path.commonpath((current, normalized)) == current:
            continue
        # Docker Desktop can report a bind mount's root as root-owned even when
        # its children retain their real ownership. Mount the parent instead.
        parent = str(Path(normalized).parent) if len(Path(normalized).parts) >= 4 else normalized
        if any(os.path.commonpath((existing, parent)) == existing for existing in result):
            continue
        result = [
            existing for existing in result if os.path.commonpath((parent, existing)) != parent
        ]
        result.append(parent)
    return result


if __name__ == "__main__":
    try:
        print("\n".join(preserved_mounts(Path(sys.argv[1]), Path(sys.argv[2]))))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
