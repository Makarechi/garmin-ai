"""Compare intraday replay, retained overlaps, and tombstones with pinned Python."""

import argparse
import importlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import ingest
from garmin_ai.models import AppState, HealthDay, Measurement, MeasurementRevision, SourcePayload
from garmin_ai.reconciliation import Replacement
from garmin_ai.replay import run_replay

ACCOUNT = "a" * 64
CLOCK = datetime(2026, 11, 1, 12, tzinfo=UTC)
ZONE = "Europe/Bratislava"


def point(day, minute, value):
    stamp = datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(minutes=minute)
    return [int(stamp.timestamp() * 1000), value]


def seed(engine, archive):
    with Session(engine) as session, session.begin():
        session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))
        module = importlib.import_module("garmin_ai.ingest")
        normalize = module.normalize

        def old_parser(session, endpoint, key, payload, ref, timezone):
            values = payload.get("heartRateValues", [])
            replaced = [[stamp, 151 if value is None else value] for stamp, value in values]
            return normalize(
                session, endpoint, key, {**payload, "heartRateValues": replaced}, ref, timezone
            )

        def write(day, points, minute, *, old=False, replacement=None, rest=None):
            document = {"heartRateValues": points} if points else {}
            if rest is not None:
                document["restingHeartRate"] = rest
            with patch.object(module, "normalize", old_parser if old else normalize):
                outcome = ingest(
                    session,
                    archive,
                    "heart_rate",
                    day,
                    document,
                    ZONE,
                    fetched_at=CLOCK + timedelta(minutes=minute),
                    replacement=replacement,
                )
            assert outcome["status"] in {"normalized", "empty"}, outcome
            return outcome["source_ref"]

        def mark_old(reference):
            row = session.get(SourcePayload, UUID(reference))
            row.parser_version = 8
            return row

        day = "2026-10-18"
        write(day, [point(day, 1, 70), point(day, 2, 73)], 1)
        fallback = write(day, [point(day, 1, None), point(day, 3, 76)], 2, old=True)
        mark_old(fallback)

        day = "2026-10-19"
        timestamp = datetime(2026, 10, 19, tzinfo=UTC)
        retained = write(day, [point(day, 1, 63)], 3, rest=49)
        later = Replacement(
            timestamp + timedelta(minutes=1),
            timestamp + timedelta(minutes=2),
            ("heart_rate_bpm",),
            "synthetic complete window",
        )
        write(day, [], 5, replacement=later)
        mark_old(retained)

        day = "2026-10-20"
        write(day, [point(day, 1, 80)], 6)
        unknown = write(day, [point(day, 1, None), point(day, 2, 75)], 7, old=True)
        mark_old(unknown)
        journal = session.get(AppState, f"ingest-history:garmin_connect:heart_rate:{day}")
        journal.value = {
            "applications": [
                {"legacy_order_unknown": True},
                journal.value["applications"][-1],
            ]
        }

        day = "2026-10-21"
        previous = write(day, [point(day, 1, 90)], 8)
        corrupt = write(day, [point(day, 1, None), point(day, 2, 77)], 9, old=True)
        mark_old(corrupt)
        old = session.get(SourcePayload, UUID(previous))
        path = archive.root / old.archive_key
        original = path.read_bytes()
        assert b", 90]" in original
        path.write_bytes(original.replace(b", 90]", b", 91]"))

        day = "2026-10-22"
        order_old = write(day, [point(day, 1, 65), point(day, 2, 66)], 10)
        write(day, [point(day, 1, 68)], 11)
        assert write(day, [point(day, 1, 65), point(day, 2, 66)], 12) == order_old
        mark_old(order_old)
        return {
            "fallback": fallback,
            "retained": retained,
            "unknown": unknown,
            "corrupt": corrupt,
            "a_b_a": order_old,
        }


def apply_python(engine, archive, refs):
    expected = {
        "fallback": "normalized",
        "retained": "normalized",
        "unknown": "error",
        "corrupt": "error",
        "a_b_a": "normalized",
    }
    for name, identity in refs.items():
        payload = {"raw_ref": identity, "target_version": 9, "account": ACCOUNT}
        try:
            result = run_replay(engine, archive, SimpleNamespace(timezone=ZONE), payload)
        except RuntimeError:
            with Session(engine) as session:
                result = session.get(AppState, f"replay:{identity}:9").value
        assert result["status"] == expected[name], (name, result)
    return expected


def snapshot(engine):
    with Session(engine) as session:
        raws = session.scalars(select(SourcePayload)).all()
        refs = {str(raw.id): raw.source_key + ":" + raw.payload_hash for raw in raws}

        def clock(value):
            if value > CLOCK + timedelta(days=1):
                return "rebuild_knowledge_boundary"
            return value.isoformat()

        def state(value):
            if isinstance(value, str):
                for reference, label in refs.items():
                    value = value.replace(reference, label)
                return value
            if isinstance(value, dict):
                return {state(key): state(item) for key, item in value.items()}
            if isinstance(value, list):
                return [state(item) for item in value]
            return value

        current = {
            (row.ts.isoformat(), row.metric): (
                row.value,
                row.local_date.isoformat(),
                refs[str(row.source_ref)],
                clock(row.ingested_at),
            )
            for row in session.scalars(select(Measurement))
        }
        revisions = sorted(
            (
                row.ts.isoformat(),
                row.metric,
                row.value,
                row.local_date.isoformat(),
                refs[str(row.source_ref)],
                clock(row.ingested_at),
                row.deleted,
            )
            for row in session.scalars(select(MeasurementRevision))
        )
        journals = {
            row.key: state(row.value)
            for row in session.scalars(select(AppState))
            if row.key.startswith(("ingest-history:", "sample-owner:"))
        }
        flags = {refs[str(raw.id)]: (raw.status, raw.parser_version) for raw in raws}
        fields = {
            row.day.isoformat(): (row.resting_hr, state(row.sources))
            for row in session.scalars(select(HealthDay))
        }
        return {
            "current": current,
            "revisions": revisions,
            "journals": journals,
            "raw": flags,
            "fields": fields,
        }


def run(binary):
    python_url = os.environ["GA_TEST_DATABASE_URL"]
    rust_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    python = isolated_engine()
    try:
        os.environ["GA_TEST_DATABASE_URL"] = rust_url
        rust = isolated_engine()
        try:
            with TemporaryDirectory() as directory:
                base = Path(directory)
                original_archive = LocalArchive(base / "python")
                native_archive = LocalArchive(base / "rust")
                original = seed(python, original_archive)
                refs = seed(rust, native_archive)
                expected = apply_python(python, original_archive, original)
                fixture = base / "cases.json"
                fixture.write_text(
                    json.dumps(
                        {"refs": refs, "expected": expected, "archive": str(native_archive.root)}
                    ),
                    encoding="utf-8",
                )
                subprocess.run(
                    [binary, "--ignored"],
                    env={**os.environ, "GA_TEST_INTRADAY_REPLAY": str(fixture)},
                    check=True,
                )
                left, right = snapshot(python), snapshot(rust)
                for key in left:
                    assert left[key] == right[key], (key, left[key], right[key])
                print(
                    f"Matched intraday replay outcomes and retained history for {len(refs)} cases"
                )
        finally:
            rust.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = python_url
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
