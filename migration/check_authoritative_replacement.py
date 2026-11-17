"""Compare scoped authoritative replacement with the pinned Python application."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_health_publication import snapshot
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import ingest
from garmin_ai.models import AppState, Insight, Measurement, MeasurementRevision
from garmin_ai.reconciliation import Replacement

ACCOUNT = "a" * 64
ZONE = "Europe/Bratislava"
BASE = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
DAY = "2026-10-25"
AFTER = "2026-10-26"


def fixture():
    def payload(values):
        return {
            "heartRateValues": [
                [int((BASE + timedelta(minutes=minute)).timestamp() * 1000), rate]
                for minute, rate in values
            ]
        }

    def replacement(note):
        return Replacement(
            BASE + timedelta(minutes=1),
            BASE + timedelta(minutes=3),
            ("heart_rate_bpm",),
            note,
        ).serialize()

    return [
        ("initial", DAY, 1, payload([(1, 70), (2, 71), (3, 72)]), None, "normalized"),
        ("partial", DAY, 2, payload([(2, 73)]), None, "normalized"),
        ("other_window", AFTER, 50, payload([(2, 87)]), None, "normalized"),
        ("replace", DAY, 30, payload([(1, 76)]), replacement("synthetic window"), "normalized"),
        ("same_input", DAY, 31, payload([(1, 76)]), replacement("synthetic window"), "unchanged"),
        (
            "same_projection",
            DAY,
            32,
            payload([(1, 76)]),
            replacement("synthetic complete retry"),
            "normalized",
        ),
        ("late_failure", DAY, 40, payload([(1, 88)]), replacement("synthetic failed"), "error"),
        ("empty_replacement", DAY, 60, {}, replacement("synthetic empty"), "empty"),
        ("stale", DAY, 59, payload([(1, 71)]), replacement("synthetic stale"), "stale"),
    ]


def apply_python(engine, archive, steps):
    with Session(engine) as session, session.begin():
        session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))
    for name, day, seconds, document, proof, expected in steps:
        with Session(engine) as session, session.begin():
            if name == "same_projection":
                session.add(
                    Insight(
                        category="synthetic",
                        statement="synthetic previous result",
                        evidence={},
                        sample_size=1,
                        status="candidate",
                        dedup_key="synthetic-attestation",
                    )
                )
                session.flush()
            if name == "late_failure":
                session.execute(
                    text(
                        "ALTER TABLE measurements ADD CONSTRAINT synthetic_attest CHECK (value!=88)"
                    )
                )
                prior = session.scalar(
                    select(MeasurementRevision.id).where(MeasurementRevision.value == 88)
                )
                assert prior is None
            result = ingest(
                session,
                archive,
                "heart_rate",
                day,
                document,
                ZONE,
                fetched_at=BASE + timedelta(seconds=seconds),
                replacement=Replacement.restore(proof),
            )
            assert result["status"] == expected, (name, result)
            if name == "same_projection":
                assert session.scalar(select(Insight.status)) == "candidate"
            if name == "late_failure":
                assert session.scalar(select(Insight.status)) == "candidate"
                assert sorted(session.scalars(select(Measurement.value))) == [72, 76, 87]
                assert (
                    session.scalar(
                        select(MeasurementRevision.id).where(MeasurementRevision.value == 88)
                    )
                    is None
                )
                session.execute(text("ALTER TABLE measurements DROP CONSTRAINT synthetic_attest"))
            if name == "empty_replacement":
                assert session.scalar(select(Insight.status)) == "superseded"


def run(binary):
    python_url = os.environ["GA_TEST_DATABASE_URL"]
    native_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    python = isolated_engine()
    try:
        os.environ["GA_TEST_DATABASE_URL"] = native_url
        native = isolated_engine()
        try:
            steps = fixture()
            with TemporaryDirectory() as directory:
                root = Path(directory)
                apply_python(python, LocalArchive(root / "archive"), steps)
                cases = root / "attested.json"
                cases.write_text(
                    json.dumps({"at": BASE.isoformat(), "steps": steps}), encoding="utf-8"
                )
                subprocess.run(
                    [binary, "--ignored", "--nocapture"],
                    check=True,
                    env={**os.environ, "GA_TEST_AUTHORITATIVE_REPLACEMENT": str(cases)},
                )
            left, right = snapshot(python), snapshot(native)
            for key, value in left.items():
                assert value == right[key], (key, value, right[key])
            print(f"Matched authoritative projections and history across {len(steps)} attempts")
        finally:
            native.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = python_url
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
