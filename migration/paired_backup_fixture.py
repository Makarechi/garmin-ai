"""Prepare or check the isolated archive and stores used by the paired backup workload."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from backup_workload import RESTORE_TEST, seed_data
from sqlalchemy import select

from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.models import Event

ROOT = Path("/backup-fixture")


def fixture(size_mib):
    settings = Settings(_env_file=None, database_url=os.environ["GA_BACKUP_DATABASE_URL"])
    if settings.data_dir != ROOT / "data":
        raise ValueError("The backup workload requires its private synthetic archive")
    engine = make_engine(settings)
    if engine.url.database != "garmin_resource_backup_test" or engine.url.query:
        engine.dispose()
        raise ValueError("The backup workload requires its disposable resource database")
    try:
        identity, digest = seed_data(engine, settings, size_mib)
        print(json.dumps({"event": str(identity), "raw_sha256": digest}))
    finally:
        engine.dispose()


def verify(expected):
    settings = Settings(_env_file=None, database_url=os.environ["GA_RESTORE_DATABASE_URL"])
    engine = make_engine(settings)
    if engine.url.database != RESTORE_TEST or engine.url.query:
        engine.dispose()
        raise ValueError("The backup workload requires its disposable recovery database")
    try:
        with transaction(engine) as session:
            event = session.scalar(select(Event).where(Event.id == UUID(expected["event"])))
            if event is None or event.idempotency_key != "synthetic-backup-benchmark":
                raise RuntimeError("The recovered backup lost its synthetic diary identity")
        backup = json.loads((ROOT / "backup-counts.json").read_text())
        exported = json.loads((ROOT / "export-counts.json").read_text())
        restored = json.loads((ROOT / "restore-counts.json").read_text())
        if not backup or not backup == exported == restored:
            raise RuntimeError("Backup, export and restore reported different table counts")
        estimate = json.loads((ROOT / "space.json").read_text())
        if estimate["status"] != "insufficient":
            raise RuntimeError("The genuinely constrained mount did not report insufficient space")
        with (ROOT / "unpacked/raw/synthetic.bin").open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if digest != expected["raw_sha256"]:
            raise RuntimeError("The restored archive bytes differ")
        print(
            json.dumps(
                {
                    "raw_sha256": digest,
                    "restored_rows": restored,
                    "encrypted_bytes": (ROOT / "complete.enc").stat().st_size,
                    "low_capacity": estimate,
                    "no_partial_destination": True,
                }
            )
        )
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("value")
    args = parser.parse_args()
    if args.mode == "prepare":
        fixture(int(args.value))
    else:
        verify(json.loads(args.value))


if __name__ == "__main__":
    main()
