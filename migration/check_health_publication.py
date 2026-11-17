"""Compare durable health projections from Python and the native worker."""

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import ingest
from garmin_ai.models import (
    AppState,
    HealthDay,
    Measurement,
    MeasurementRevision,
    MetricObservation,
    SourcePayload,
    TimelineInterval,
)

DAY = "2026-10-25"
ZONE = "Europe/Bratislava"
ACCOUNT = "a" * 64
STOP = """
ALTER TABLE measurements ADD CONSTRAINT synthetic_health_error
CHECK (value != 88)
"""
RESUME = "ALTER TABLE measurements DROP CONSTRAINT synthetic_health_error"


def apply_python(engine, fixture):
    with TemporaryDirectory() as temporary:
        archive = LocalArchive(Path(temporary))
        with Session(engine) as session, session.begin():
            session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))
        for step in fixture:
            with Session(engine) as session, session.begin():
                if step.get("force_failure"):
                    session.execute(text(STOP))
                result = ingest(
                    session,
                    archive,
                    step["endpoint"],
                    DAY,
                    step["payload"],
                    ZONE,
                    fetched_at=datetime.fromtimestamp(1_790_000_000 + step["second"], UTC),
                )
                assert result["status"] == step["status"], result
                if step.get("force_failure"):
                    assert (
                        session.scalar(
                            select(HealthDay.resting_hr).where(
                                HealthDay.day == date.fromisoformat(DAY)
                            )
                        )
                        == 59
                    )
                    assert not session.scalar(
                        select(MeasurementRevision.id).where(MeasurementRevision.value == 88)
                    )
                    session.execute(text(RESUME))


def snapshot(engine):
    with Session(engine) as session, session.begin():
        references = {
            str(row.id): f"{row.endpoint}:{row.payload_hash}"
            for row in session.scalars(select(SourcePayload))
        }

        def clean(value):
            if isinstance(value, str):
                for before, after in references.items():
                    value = value.replace(before, after)
                return value
            if isinstance(value, dict):
                return {clean(key): clean(value) for key, value in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        def rows(model, ignored=(), order=()):
            return [
                clean(
                    {
                        column.name: str(value) if column.name == "source_ref" else value
                        for column in model.__table__.columns
                        if column.name not in ignored
                        for value in [getattr(record, column.name)]
                    }
                )
                for record in session.scalars(select(model).order_by(*order))
            ]

        return {
            "daily": rows(HealthDay, ["updated_at"], [HealthDay.day]),
            "current": rows(
                Measurement, ["metric_definition_version_id"], [Measurement.ts, Measurement.metric]
            ),
            "revisions": rows(
                MeasurementRevision,
                ["id", "metric_definition_version_id"],
                [
                    MeasurementRevision.ts,
                    MeasurementRevision.ingested_at,
                    MeasurementRevision.metric,
                ],
            ),
            "observations": rows(
                MetricObservation,
                ["id", "metric_definition_version_id", "ingested_at"],
                [MetricObservation.metric, MetricObservation.sequence],
            ),
            "timeline": rows(TimelineInterval, [], [TimelineInterval.id]),
            "history": clean(
                {
                    row.key: row.value
                    for row in session.scalars(
                        select(AppState).where(AppState.key.startswith("ingest-history:"))
                    )
                }
            ),
        }


def run(binary, fixtures):
    python_target = os.environ["GA_TEST_DATABASE_URL"]
    rust_target = os.environ["GA_TEST_RUST_DATABASE_URL"]
    if python_target == rust_target:
        raise ValueError("Use two distinct disposable databases")
    python = isolated_engine()
    try:
        apply_python(python, json.loads(fixtures.read_text(encoding="utf-8")))
        os.environ["GA_TEST_DATABASE_URL"] = rust_target
        rust = isolated_engine()
        try:
            subprocess.run([binary, "--ignored"], check=True)
            native = snapshot(rust)
            expected = snapshot(python)
            for name, value in expected.items():
                assert native[name] == value, (name, native[name], value)
            print("Durable daily fields, samples, revisions, observations and history match Python")
        finally:
            rust.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = python_target
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("fixtures", type=Path)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.fixtures.resolve(strict=True))
