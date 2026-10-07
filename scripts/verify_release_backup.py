"""Authenticate a bundle backup by unpacking to an ephemeral private directory."""

import shutil
import sys
from pathlib import Path
from uuid import uuid4

from garmin_ai.config import Settings
from garmin_ai.operations import unpack_backup


def main():
    settings = Settings()
    source = Path(sys.argv[1])
    if source.parent != settings.backup_dir or source.suffix != ".enc":
        raise SystemExit("Backup verification accepts an encrypted file in GA_BACKUP_DIR")
    destination = settings.backup_dir / (".verify-" + uuid4().hex)
    try:
        unpack_backup(settings, source, destination)
        if not (destination / "database.jsonl.gz").is_file():
            raise ValueError("Backup has no database snapshot")
        print("Encrypted backup authentication and unpack verification passed.")
    finally:
        if destination.is_dir():
            shutil.rmtree(destination)


if __name__ == "__main__":
    main()
