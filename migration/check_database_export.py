"""Export a pinned Python fixture and restore the native snapshot with pinned Python."""

import argparse
import gzip
import json
import math
import os
import shutil
import subprocess
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.canonical_events import backfill_canonical_events
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState, Base, HealthDay, Measurement, SourcePayload
from garmin_ai.operations import REVISION, export_database, restore_database

AT = datetime(2026, 10, 25, 1, 30, 0, 123000, UTC)
SOURCE = UUID(int=850)


def seed(engine):
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                AppState(key="test:backup-export", value={"fixture": True}),
                HealthDay(
                    day=date(2026, 10, 25),
                    sleep_score=82.125,
                    deep_seconds=-0.0,
                    resting_hr=float("inf"),
                    steps=5432,
                    sources={"label": "Синтетический тест ☕ \"-0\"", "none": None},
                    updated_at=AT,
                ),
                SourcePayload(
                    id=SOURCE,
                    endpoint="synthetic",
                    source_key="test",
                    payload_hash="a" * 64,
                    archive_key="synthetic/source.json",
                    payload={"nested": ["тест", 1.125, None, True]},
                    fetched_at=AT,
                    parser_version=12,
                    status="parsed",
                ),
                Measurement(
                    ts=AT,
                    metric="synthetic_metric",
                    local_date=date(2026, 10, 25),
                    value=0.0625,
                    unit="test",
                    source_ref=SOURCE,
                    details={"revision": 2},
                    ingested_at=AT,
                ),
            ]
        )
        create_event(
            session,
            EventInput(
                start=AT,
                timezone="Europe/Bratislava",
                original_text="Синтетическая запись ☕",
                payload={"type": "note", "description": "archive\nround trip", "tags": ["test"]},
            ),
            actor="api",
            idempotency_key="synthetic-export-event",
        )
        backfill_canonical_events(session)


def inspect_export(path):
    observed = Counter()
    order = []
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        header = json.loads(next(stream))
        assert header["format"] == "garmin-ai-jsonl-v1"
        assert header["revision"] == REVISION
        assert datetime.fromisoformat(header["exported_at"]).utcoffset() is not None
        for line in stream:
            record = json.loads(line)
            if "counts" in record:
                assert not stream.read()
                counts = record["counts"]
                assert all(count == observed[table] for table, count in counts.items())
                return order, counts
            table = record["table"]
            if not order or order[-1] != table:
                order.append(table)
            if table == "app_state":
                assert record["row"]["key"] != "test:committed-after-header"
            observed[table] += 1
    raise AssertionError("The export footer is missing")


def table_values(engine):
    with engine.connect() as connection:
        return {
            table.name: [
                dict(row) for row in connection.execute(select(table).order_by(*table.primary_key))
                .mappings()
            ]
            for table in Base.metadata.sorted_tables
        }


def run(binary):
    source_url = make_url(os.environ["GA_TEST_DATABASE_URL"])
    destination_url = os.environ["GA_TEST_RESTORE_DATABASE_URL"]
    target = make_url(destination_url)
    assert source_url.set(database=target.database) == target
    assert source_url.database != target.database
    engine = isolated_engine()
    try:
        with patch.dict(os.environ, {"GA_TEST_DATABASE_URL": destination_url}):
            restored = isolated_engine()
        try:
            seed(engine)
            with TemporaryDirectory() as directory:
                root = Path(directory)
                reference = root / "reference.jsonl.gz"
                native = root / "native.jsonl"
                compressed = root / "native.jsonl.gz"
                export_database(engine, reference, settings=Settings(_env_file=None))
                original = table_values(engine)
                subprocess.run(
                    [binary, "--ignored", "--nocapture"],
                    env={**os.environ, "GA_TEST_DATABASE_EXPORT_OUTPUT": str(native)},
                    check=True,
                )
                with native.open("rb") as raw, gzip.open(compressed, "wb") as output:
                    shutil.copyfileobj(raw, output, length=65536)
                order, counts = inspect_export(compressed)
                assert (order, counts) == inspect_export(reference)
                assert set(counts) == {table.name for table in Base.metadata.sorted_tables}
                assert restore_database(restored, compressed) == counts
                recovered = table_values(restored)
                assert recovered.keys() == original.keys()
                for table in original:
                    assert recovered[table] == original[table], table
                original_zero = original["health_days"][0]["deep_seconds"]
                restored_zero = recovered["health_days"][0]["deep_seconds"]
                assert math.copysign(1, original_zero) == math.copysign(1, restored_zero)
                with restored.begin() as connection:
                    previous = max(row["id"] for row in recovered["audit_log"])
                    statement = text(
                        "INSERT INTO audit_log(event_id,action,actor) "
                        "SELECT id,'synthetic','api' FROM events RETURNING id"
                    )
                    assert connection.scalar(statement) > previous
        finally:
            restored.dispose()
    finally:
        engine.dispose()
    print(f"Python restored all {len(counts)} tables and {sum(counts.values())} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    run(parser.parse_args().binary)
