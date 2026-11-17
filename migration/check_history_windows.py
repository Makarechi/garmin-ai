"""Compare native and pinned Python scheduling of Garmin's daily history windows."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from capture_reference import isolated_engine
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.backfill import schedule_history
from garmin_ai.models import AppState, Job

ACCOUNT = "a" * 64
ZONE = "Europe/Bratislava"
STEPS = [
    ("start", "2026-10-25T23:35:00+00:00", 5),
    ("continue", "2026-10-25T23:36:00+00:00", 5),
    ("finish", "2026-10-25T23:37:00+00:00", 5),
    ("retry_first", "2026-10-25T23:38:00+00:00", 5),
    ("retry_next", "2026-10-25T23:39:00+00:00", 5),
    ("outage", "2026-10-30T23:35:00+00:00", 5),
    ("outage_next", "2026-10-30T23:36:00+00:00", 5),
    ("restrict", "2026-10-30T23:37:00+00:00", 5),
    ("expanded_horizon", "2026-10-30T23:38:00+00:00", 12),
    ("more_old_days", "2026-10-30T23:39:00+00:00", 12),
    ("disabled", "2026-10-31T23:39:00+00:00", 0),
]


def seed(engine):
    with Session(engine) as session, session.begin():
        session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))


def prepare(session, step):
    if step == "retry_first":
        session.execute(
            text(
                "UPDATE app_state SET value=value || jsonb_build_object('status','disabled')"
                " WHERE key IN (SELECT key FROM app_state WHERE starts_with(key,'syncwindow:')"
                " ORDER BY key LIMIT 3)"
            )
        )
    if step == "restrict":
        session.execute(
            text(
                "INSERT INTO module_configs(id, owner_id, pack_key, revision, tracking_enabled, "
                "collection_enabled, reminders_enabled, visible, llm_enabled, settings)"
                " SELECT gen_random_uuid(), id, 'wellbeing',1,false,true,false,false,false,'{}'"
                " FROM people"
            )
        )


def snapshot(engine):
    with Session(engine) as session:
        jobs = list(session.scalars(select(Job).order_by(Job.run_at, Job.dedup_key)))
        identities = {
            str(job.id): (
                job.dedup_key.split(":retry:")[0] + ":retry"
                if ":retry:" in job.dedup_key
                else job.dedup_key
            )
            for job in jobs
        }
        plans = {}
        windows = {}
        for state in session.scalars(select(AppState).order_by(AppState.key)):
            if state.key.startswith("syncplan:"):
                plans[state.key] = state.value
            elif state.key.startswith("syncwindow:"):
                data = dict(state.value)
                if identity := data.get("job_id"):
                    data["job_id"] = identities[identity]
                windows[state.key] = data
        return {
            "plans": plans,
            "windows": windows,
            "jobs": [
                {
                    "kind": job.kind,
                    "dedup": identities[str(job.id)],
                    "payload": job.payload,
                    "run_at": job.run_at,
                    "status": job.status,
                }
                for job in jobs
            ],
        }


def run(binary):
    python_url = os.environ["GA_TEST_DATABASE_URL"]
    native_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    python = isolated_engine()
    try:
        os.environ["GA_TEST_DATABASE_URL"] = native_url
        native = isolated_engine()
        try:
            seed(python)
            seed(native)
            expected_counts = []
            for name, at, horizon in STEPS:
                before = len(snapshot(python)["jobs"])
                with Session(python) as session, session.begin():
                    prepare(session, name)
                    schedule_history(
                        session,
                        SimpleNamespace(timezone=ZONE, backfill_days=horizon),
                        datetime.fromisoformat(at).astimezone(UTC),
                    )
                expected_counts.append((name, at, horizon, len(snapshot(python)["jobs"]) - before))
            with TemporaryDirectory() as directory:
                path = Path(directory) / "history-windows.json"
                path.write_text(json.dumps(expected_counts), encoding="utf-8")
                subprocess.run(
                    [binary, "--ignored", "--nocapture"],
                    check=True,
                    env={**os.environ, "GA_TEST_HISTORY_WINDOWS": str(path)},
                )
            left, right = snapshot(python), snapshot(native)
            for key in left:
                assert left[key] == right[key], key
            print(f"Matched {len(STEPS)} scheduling passes and {len(left['jobs'])} jobs")
        finally:
            native.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = python_url
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
