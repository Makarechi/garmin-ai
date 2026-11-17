"""Measure encrypted backup, restore and real low-capacity handling on synthetic data."""

import argparse
import base64
import hashlib
import json
import random
import secrets
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select

from garmin_ai import operations
from garmin_ai.backup_space import BackupSpaceInsufficient, backup_space
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import Event

RESTORE_TEST = "garmin_resource_restore_test"


def seed_data(engine, settings, size_mib):
    raw = settings.data_dir / "raw"
    raw.mkdir(mode=0o700, parents=True)
    source = raw / "synthetic.bin"
    generator = random.Random(82139)
    digest = hashlib.sha256()
    with source.open("wb") as target:
        for _ in range(size_mib):
            chunk = generator.randbytes(1024 * 1024)
            target.write(chunk)
            digest.update(chunk)
    with transaction(engine) as session:
        row = create_event(
            session,
            EventInput(
                start="2026-09-01T10:00:00+00:00",
                timezone="UTC",
                payload={"type": "note", "description": "Synthetic benchmark note"},
            ),
            actor="api",
            idempotency_key="synthetic-backup-benchmark",
        )
        return row.id, digest.hexdigest()


def initialize_restore(engine):
    root = Path(operations.__file__).parent / "migrations"
    with engine.begin() as conn:
        config = Config()
        config.set_main_option("script_location", str(root))
        config.attributes["connection"] = conn
        command.upgrade(config, "head")


def run(size_mib):
    settings = Settings(
        _env_file=None,
        backup_key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    )
    source_engine = make_engine(settings)
    if not (source_engine.url.database or "").endswith("_test") or source_engine.url.query:
        raise ValueError("The backup workload requires a dedicated synthetic test database")
    restore_engine = create_engine(
        source_engine.url.set(database=RESTORE_TEST), hide_parameters=True
    )
    try:
        event_id, expected_digest = seed_data(source_engine, settings, size_mib)
        initialize_restore(restore_engine)
        low_destination = Path("/tmp/low-backup-space/synthetic.enc")
        shortage = backup_space(source_engine, settings, low_destination)
        if shortage["status"] != "insufficient":
            raise RuntimeError("The constrained backup volume did not report a shortage")
        try:
            operations.create_backup(source_engine, settings, low_destination)
        except BackupSpaceInsufficient:
            if low_destination.exists():
                raise RuntimeError("The failed backup published a destination") from None
        else:
            raise RuntimeError("Backup proceeded despite insufficient space")

        destination = settings.backup_dir / "synthetic.enc"
        start = time.monotonic()
        expected_counts = operations.create_backup(source_engine, settings, destination)
        backup_duration = time.monotonic() - start
        recovery_root = settings.backup_dir / "recovered"
        start = time.monotonic()
        operations.unpack_backup(settings, destination, recovery_root)
        recovered_counts = operations.restore_database(
            restore_engine, recovery_root / "database.jsonl.gz"
        )
        restore_duration = time.monotonic() - start
        with (recovery_root / "raw/synthetic.bin").open("rb") as stream:
            actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        with transaction(restore_engine) as session:
            restored = session.scalar(select(Event).where(Event.id == event_id))
            if restored is None or restored.idempotency_key != "synthetic-backup-benchmark":
                raise RuntimeError("The restored database did not keep the diary identity")
        if actual_digest != expected_digest or recovered_counts != expected_counts:
            raise RuntimeError("The recovered archive or table counts differ")
        return {
            "synthetic_raw_bytes": size_mib * 1024 * 1024,
            "encrypted_bytes": destination.stat().st_size,
            "backup_seconds": round(backup_duration, 3),
            "authenticate_unpack_restore_seconds": round(restore_duration, 3),
            "raw_sha256": actual_digest,
            "restored_rows": recovered_counts,
            "low_capacity": shortage,
        }
    finally:
        source_engine.dispose()
        restore_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("size_mib", type=int, choices=(32, 128))
    options = parser.parse_args()
    print(json.dumps(run(options.size_mib)))
