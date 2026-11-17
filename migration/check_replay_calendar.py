"""Compare archived calendar-day replay with the pinned Python application."""

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from garmin_ai import replay
from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import ingest
from garmin_ai.models import (
    AppState,
    HealthDay,
    MetricObservation,
    SourcePayload,
    TimelineInterval,
)

ACCOUNT = "a" * 64
ORIGINAL_ZONE = "Europe/Bratislava"
CURRENT_ZONE = "America/New_York"
AT = datetime(2026, 10, 25, 9, tzinfo=UTC)


def seed(engine, archive):
    with Session(engine) as session, session.begin():
        session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))

        def write(endpoint, day, payload, minute, zone=ORIGINAL_ZONE):
            result = ingest(
                session,
                archive,
                endpoint,
                day,
                payload,
                zone,
                fetched_at=AT + timedelta(minutes=minute),
            )
            assert result["status"] in {"normalized", "empty", "archived"}, result
            return result["source_ref"]

        def mark_old(reference):
            row = session.get(SourcePayload, UUID(reference))
            row.parser_version = 8
            return row

        retained = write("daily", "2026-10-22", {"totalSteps": 75, "restingHeartRate": 51}, 0)
        write("daily", "2026-10-22", {"totalSteps": 250}, 12)
        mark_old(retained)
        ordinary = write("daily", "2026-10-23", {"totalSteps": 85}, 1)
        mark_old(ordinary)
        row = session.get(HealthDay, date(2026, 10, 23))
        row.stress_max = 63
        row.sources = {
            **row.sources,
            "field:stress_max": ordinary,
            "time:stress_max": (AT + timedelta(minutes=1)).isoformat(),
        }

        def sleep(day, starts, ends, deep, score):
            dto = {
                "calendarDate": day,
                "sleepStartTimestampGMT": starts,
                "sleepEndTimestampGMT": ends,
                "sleepScores": {"overall": {"value": score}},
            }
            if deep is not None:
                dto["deepSleepSeconds"] = deep
            return {"dailySleepDTO": dto}

        autumn = sleep("2026-10-25", "2026-10-24T21:35:10.119Z", "2026-10-25T06:17:00Z", 555, 71)
        other = sleep("2026-10-25", "2026-10-24T21:20:00Z", "2026-10-25T06:03:00Z", 552, 83)
        active = write("sleep", "2026-10-25", autumn, 3)
        write("sleep", "2026-10-25", other, 17)
        assert write("sleep", "2026-10-25", autumn, 25, "Europe/Prague") == active
        mark_old(active)

        prior = sleep("2026-10-26", "2026-10-25T22:00:00Z", "2026-10-26T05:20:00Z", 440, 80)
        latest = sleep("2026-10-26", "2026-10-25T21:45:00Z", "2026-10-26T05:53:00Z", None, 84)
        retained_sleep = write("sleep", "2026-10-26", prior, 30)
        write("sleep", "2026-10-26", latest, 39)
        mark_old(retained_sleep)
        empty = write("hydration", "2026-10-26", {}, 40)
        mark_old(empty)
        damaged = write("daily", "2026-10-27", {"totalSteps": 92}, 41)
        damaged_row = mark_old(damaged)
        path = archive.root / damaged_row.archive_key
        body = path.read_bytes()
        path.write_bytes(body.replace(b"92", b"93"))
        superseded = write("daily", "2026-10-28", {"totalSteps": 100}, 43)
        write("daily", "2026-10-28", {"totalSteps": 184}, 45)
        mark_old(superseded)

        tied_daily = write("daily", "2026-10-30", {"restingHeartRate": 50}, 53)
        tied_heart = write("heart_rate", "2026-10-30", {"restingHeartRate": 60}, 53)
        tied_day = session.get(HealthDay, date(2026, 10, 30))
        assert tied_day.resting_hr == 60 and tied_day.sources["field:resting_hr"] == tied_heart
        mark_old(tied_daily)

        old_without_interval = sleep(
            "2026-10-31", "2026-10-30T21:00:00Z", "2026-10-31T04:20:00Z", None, 67
        )
        missing_interval = write("sleep", "2026-10-31", old_without_interval, 54)
        session.execute(delete(TimelineInterval).where(TimelineInterval.id == "sleep:2026-10-31"))
        write("sleep", "2026-10-31", {"dailySleepDTO": {"sleepTimeSeconds": 400}}, 55)
        assert session.get(TimelineInterval, "sleep:2026-10-31") is None
        mark_old(missing_interval)

        forced = write("daily", "2026-10-29", {"totalSteps": 19000}, 47)
        mark_old(forced)
        session.execute(
            update(MetricObservation).values(
                ingested_at=MetricObservation.fetched_at + timedelta(minutes=5)
            )
        )
        session.add(
            AppState(
                key="analysis:conversation:pending:synthetic-replay", value={"synthetic": True}
            )
        )
        return {
            "ordinary": ordinary,
            "retained": retained,
            "active_sleep": active,
            "old_sleep": retained_sleep,
            "empty": empty,
            "damaged": damaged,
            "superseded": superseded,
            "forced": forced,
            "tied_daily": tied_daily,
            "missing_interval": missing_interval,
        }


def apply_python(engine, archive, refs):
    expected = {
        "ordinary": "normalized",
        "retained": "normalized",
        "active_sleep": "normalized",
        "old_sleep": "normalized",
        "empty": "empty",
        "damaged": "error",
        "superseded": "superseded_revision",
        "forced": "normalized",
        "tied_daily": "normalized",
        "missing_interval": "normalized",
    }
    for name, identity in refs.items():
        args = {"raw_ref": identity, "target_version": 9, "account": ACCOUNT}
        try:
            result = replay.run_replay(
                engine, archive, SimpleNamespace(timezone=CURRENT_ZONE), args
            )
        except RuntimeError:
            with Session(engine) as session:
                result = session.get(AppState, f"replay:{identity}:9").value
        assert result["status"] == expected[name], (name, result["status"])
    return expected


def snapshot(engine):
    with Session(engine) as session:
        raws = session.scalars(
            select(SourcePayload).order_by(
                SourcePayload.endpoint, SourcePayload.source_key, SourcePayload.payload_hash
            )
        ).all()
        refs = {str(row.id): f"{row.endpoint}:{row.source_key}:{row.payload_hash}" for row in raws}

        def stable(value):
            if isinstance(value, str):
                for reference, label in refs.items():
                    value = value.replace(reference, label)
                return value
            if isinstance(value, dict):
                return {stable(key): stable(item) for key, item in value.items()}
            if isinstance(value, list):
                return [stable(item) for item in value]
            return value

        def rows(model, excluded, order):
            return [
                stable(
                    {
                        col.name: getattr(row, col.name)
                        for col in model.__table__.columns
                        if col.name not in excluded
                    }
                )
                for row in session.scalars(select(model).order_by(*order))
            ]

        observations = rows(
            MetricObservation,
            {"id", "metric_definition_version_id", "source_ref"},
            (
                MetricObservation.source_calendar_date,
                MetricObservation.metric,
                MetricObservation.fetched_at,
                MetricObservation.sequence,
            ),
        )
        # Source record IDs are random across the two databases; preserve their
        # links by resolving each to the content identity in the matching order.
        observation_refs = session.scalars(
            select(MetricObservation.source_ref).order_by(
                MetricObservation.source_calendar_date,
                MetricObservation.metric,
                MetricObservation.fetched_at,
                MetricObservation.sequence,
            )
        ).all()
        for observation, reference in zip(observations, observation_refs, strict=True):
            observation["source_ref"] = refs[str(reference)]

        tracked = {}
        for row in session.scalars(select(AppState)):
            if row.key.startswith(
                ("ingest:", "ingest-meta:", "observation-owner:", "interval-owner:")
            ):
                value = dict(row.value)
                if row.key.startswith("ingest:"):
                    value.pop("fetched_at", None)  # Processing clock varies across real runs.
                tracked[stable(row.key)] = stable(value)
            if row.key.startswith("replay:") and row.key != "replay:generation":
                value = dict(row.value)
                assert datetime.fromisoformat(value.pop("at")).utcoffset() is not None
                tracked[stable(row.key)] = stable(value)

        return {
            "status": [(refs[str(raw.id)], raw.status, raw.parser_version) for raw in raws],
            "daily": rows(HealthDay, {"updated_at"}, (HealthDay.day,)),
            "timeline": rows(TimelineInterval, set(), (TimelineInterval.id,)),
            "observations": observations,
            "provenance": tracked,
            "invalidated": bool(session.get(AppState, "replay:generation"))
            and not bool(session.get(AppState, "analysis:conversation:pending:synthetic-replay")),
        }


def run(binary):
    python_url = os.environ["GA_TEST_DATABASE_URL"]
    native_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    python = isolated_engine()
    try:
        os.environ["GA_TEST_DATABASE_URL"] = native_url
        native = isolated_engine()
        try:
            with TemporaryDirectory() as temporary:
                root = Path(temporary)
                python_archive = LocalArchive(root / "python")
                native_archive = LocalArchive(root / "native")
                expected = apply_python(python, python_archive, seed(python, python_archive))
                refs = seed(native, native_archive)
                path = root / "calendar-cases.json"
                path.write_text(
                    json.dumps(
                        {"refs": refs, "expected": expected, "archive": str(native_archive.root)}
                    ),
                    encoding="utf-8",
                )
                subprocess.run(
                    [binary, "--ignored", "--nocapture"],
                    check=True,
                    env={**os.environ, "GA_TEST_REPLAY_CALENDAR": str(path)},
                )
                left, right = snapshot(python), snapshot(native)
                for key in left:
                    assert left[key] == right[key], (key, left[key], right[key])
                print(f"Calendar replay matched Python across {len(expected)} operations")
        finally:
            native.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = python_url
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
