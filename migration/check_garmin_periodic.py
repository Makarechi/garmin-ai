"""Record synthetic periodic scheduling outcomes from the pinned Python runtime."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from capture_reference import isolated_engine
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.config import Settings
from garmin_ai.jobs import retire_garmin_jobs
from garmin_ai.models import AppState, Job
from garmin_ai.onboarding import source_instance_selected
from garmin_ai.sync import schedule_sync

ACCOUNT = "a" * 64
SOURCE = "source:garmin:primary"
SLEEP_SQL = """INSERT INTO timeline_intervals (id,start,"end",label,source,confidence,confirmed,evidence)
SELECT 'synthetic-wake', '2026-10-26T08:00:00+00:00','2026-10-26T13:00:00+00:00',
       'sleep','fixture',1,false,'{}'"""
ONBOARDING_SQL = """INSERT INTO app_state(key,value)
VALUES('preferences:onboarding',jsonb_build_object('source_instance_ids',jsonb_build_array('source:garmin:primary')))
ON CONFLICT (key) DO UPDATE SET value=excluded.value"""
DISABLE_SQL = """UPDATE app_state SET value=jsonb_build_object('source_instance_ids',jsonb_build_array())
WHERE key='preferences:onboarding'"""
PAUSE_SQL = """INSERT INTO app_state(key,value)
VALUES('integration:garmin',jsonb_build_object('status','reauth_required'))"""
RESTRICT_SQL = """INSERT INTO module_configs(id,owner_id,pack_key,revision,tracking_enabled,
collection_enabled,reminders_enabled,visible,llm_enabled,settings)
SELECT gen_random_uuid(),id,'wellbeing',1,false,true,false,false,false,'{}' FROM people"""
STEPS = [
    ("sunday", "2026-10-25T01:10:00+00:00", "Europe/Bratislava", []),
    ("restart", "2026-10-25T01:10:00+00:00", "Europe/Bratislava", []),
    ("monday", "2026-10-26T01:10:00+00:00", "Europe/Bratislava", []),
    ("stored_zone_and_wake", "2026-10-26T13:10:00+00:00", "UTC", [SLEEP_SQL, ONBOARDING_SQL]),
    ("evening", "2026-10-26T17:10:00+00:00", "UTC", []),
    ("paused", "2026-10-27T01:10:00+00:00", "UTC", [PAUSE_SQL]),
    ("disabled", "2026-10-27T01:11:00+00:00", "UTC", [DISABLE_SQL]),
    (
        "restricted",
        "2026-10-28T01:10:00+00:00",
        "UTC",
        [ONBOARDING_SQL, "DELETE FROM app_state WHERE key='integration:garmin'", RESTRICT_SQL],
    ),
]


def micros(value):
    if value is None:
        return None
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def snapshot(session):
    jobs = []
    lanes = []
    for job in session.scalars(select(Job).order_by(Job.dedup_key)):
        family, *_ = job.dedup_key.split(":", 1)
        if family == "activity-page":
            lanes.append([job.payload["scan_key"].split(":")[-1], job.payload["since"], job.status])
        elif family in {"frequent", "morning", "reconcile", "daily"}:
            jobs.append(
                [
                    job.dedup_key,
                    job.payload,
                    job.status,
                    job.last_error,
                    micros(job.run_at),
                    micros(job.completed_at),
                ]
            )
    windows = session.execute(
        text(
            "SELECT key,value->>'status' FROM app_state WHERE starts_with(key,'syncwindow:') "
            "ORDER BY key"
        )
    ).all()
    return {"jobs": jobs, "activity": sorted(lanes), "windows": [list(row) for row in windows]}


def run(path):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))
        cases = []
        for name, at, timezone, prepare in STEPS:
            with (
                Session(engine) as session,
                session.begin(),
                patch("garmin_ai.sync.random.uniform", return_value=0),
            ):
                for statement in prepare:
                    session.execute(text(statement))
                now = datetime.fromisoformat(at)
                if source_instance_selected(session, SOURCE):
                    schedule_sync(
                        session,
                        Settings(_env_file=None, timezone=timezone, backfill_days=2),
                        now,
                    )
                else:
                    retire_garmin_jobs(session, now)
                cases.append(
                    {
                        "name": name,
                        "at": at,
                        "timezone": timezone,
                        **snapshot(session),
                    }
                )
        assert sum(row[0].startswith("reconcile:2026-10-25:") for row in cases[0]["jobs"]) == 134
        assert sum(row[0].startswith("reconcile:2026-10-26:") for row in cases[2]["jobs"]) == 594
        assert any(row[0].startswith("morning:sleep:") for row in cases[3]["jobs"])
        assert cases[4]["jobs"] == cases[5]["jobs"]
        path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
