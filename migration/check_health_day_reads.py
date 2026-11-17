"""Compare native calendar health reads with pinned Python on synthetic data."""

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.models import HealthDay, SourcePayload
from garmin_ai.queries import health_range, health_snapshot


def capture(engine):
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                HealthDay(
                    day=date(2026, 10, 24),
                    steps=0,
                    resting_hr=53.0,
                    hydration_ml=-0.0,
                    hrv_status="synthetic",
                    updated_at=datetime(2026, 10, 25, 1, 3, 7, 130010, tzinfo=UTC),
                    sources={
                        "field:steps": "synthetic-source-ref",
                        "unknown": {"note": "Téšt", "nested": [0, False, None]},
                    },
                ),
                HealthDay(
                    day=date(2026, 10, 26),
                    awake_seconds=72.5,
                    training_status="synthetic stable",
                    training_readiness_score=0,
                    updated_at=datetime(2026, 10, 26, 9, 35, tzinfo=UTC),
                    sources={},
                ),
                SourcePayload(
                    source="garmin_connect",
                    endpoint="daily",
                    source_key="2026-10-24",
                    payload_hash="1" * 64,
                    payload={"totalSteps": 0},
                    archive_key="sha256:" + "1" * 64,
                    status="pending",
                    parser_version=0,
                    fetched_at=datetime(2026, 10, 25, tzinfo=UTC),
                ),
            ]
        )
        session.flush()
        operations = [
            ("snapshot", ["2026-10-24"]),
            ("snapshot", ["2026-10-25"]),
            ("snapshot", ["2026-10-26"]),
            ("snapshot", ["0001-01-01"]),
            ("range", ["2026-10-24", "2026-10-26"]),
            ("range", ["2026-10-25", "2026-10-25"]),
            ("range", ["2026-10-24", "2026-10-24"]),
            ("range", ["2025-01-01", "2024-01-01"]),
            ("range", ["2020-01-01", "2030-01-08"]),
            ("range", ["2020-01-01", "2030-01-09"]),
            ("range", ["0001-01-01", "0001-01-01"]),
            ("range", ["9999-11-01", "9999-11-02"]),
        ]
        result = []
        for action, args in operations:
            execute = health_snapshot if action == "snapshot" else health_range
            try:
                outcome = execute(session, *map(date.fromisoformat, args))
            except ValueError as exc:
                outcome = {"error": str(exc)}
            result.append([action, args, outcome])
        return result


def run(binary):
    engine = isolated_engine()
    try:
        cases = capture(engine)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "health-day-reads.json"
            path.write_text(json.dumps(cases, allow_nan=False), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_HEALTH_DAY_READS": str(path)},
            )
        print(f"Matched {len(cases)} calendar health read decisions")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
