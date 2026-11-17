"""Compare durable recent and history activity scans with pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from capture_reference import isolated_engine
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.activity_sync import schedule_scans
from garmin_ai.models import AppState, Job

ACCOUNT = "a" * 64
ZONE = "Europe/Bratislava"
STEPS = [
    ("recent", "2026-10-25T23:35:00+00:00", 0),
    ("history", "2026-10-25T23:36:00+00:00", 12),
    ("exhausted", "2026-10-25T23:37:00+00:00", 12),
    ("cooldown", "2026-10-25T23:38:00+00:00", 12),
    ("cooldown_retry", "2026-10-26T00:00:00+00:00", 12),
    ("disabled", "2026-10-26T23:39:00+00:00", 12),
]


def seed(engine):
    with Session(engine) as session, session.begin():
        session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))


def prepare(session, case):
    if case == "exhausted":
        session.execute(
            text(
                "UPDATE jobs SET status='failed' WHERE right(payload->>'scan_key',7)=concat(chr(58),'recent')"
            )
        )
    if case == "cooldown":
        session.execute(
            text(
                "UPDATE app_state SET value=value || jsonb_build_object("
                "'status','complete','next_scan_at','2026-10-26T00:00:00+00:00')"
                " WHERE starts_with(key,'activity_scan:') AND right(key,7)=concat(chr(58),'recent')"
            )
        )
    if case == "disabled":
        session.execute(
            text(
                "INSERT INTO module_configs(id,owner_id,pack_key,revision,tracking_enabled,"
                " collection_enabled,reminders_enabled,visible,llm_enabled,settings)"
                " SELECT gen_random_uuid(),id,'training',1,false,false,false,false,false,'{}'"
                " FROM people"
            )
        )


def snapshot(engine):
    with Session(engine) as session:
        jobs = list(session.scalars(select(Job).order_by(Job.run_at)))
        replacements = {}
        for index, job in enumerate(jobs):
            replacements[str(job.id)] = f"stable-job-{index}"
            replacements[job.payload["generation"]] = f"stable-generation-{index}"

        def stable(document):
            value = dict(document)
            for field in ("generation", "job_id"):
                if reference := value.get(field):
                    value[field] = replacements[reference]
            return value

        scans = session.scalars(
            select(AppState).where(AppState.key.startswith("activity_scan:")).order_by(AppState.key)
        )
        return {
            "scans": {scan.key: stable(scan.value) for scan in scans},
            "jobs": [
                {
                    "kind": job.kind,
                    "payload": stable(job.payload),
                    "key": job.dedup_key.replace(
                        job.payload["generation"], replacements[job.payload["generation"]]
                    ),
                    "status": job.status,
                    "attempts": job.attempts,
                    "time": job.run_at,
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
            counts = []
            for case, now, horizon in STEPS:
                before = len(snapshot(python)["jobs"])
                with Session(python) as session, session.begin():
                    prepare(session, case)
                    assert schedule_scans(
                        session,
                        SimpleNamespace(timezone=ZONE, backfill_days=horizon),
                        datetime.fromisoformat(now),
                    )
                counts.append((case, now, horizon, len(snapshot(python)["jobs"]) - before))
            with TemporaryDirectory() as directory:
                fixture = Path(directory) / "activity-scans.json"
                fixture.write_text(json.dumps(counts), encoding="utf-8")
                subprocess.run(
                    [binary, "--ignored", "--nocapture"],
                    check=True,
                    env={**os.environ, "GA_TEST_ACTIVITY_SCANS": str(fixture)},
                )
            assert snapshot(python) == snapshot(native)
            print(f"Matched {len(STEPS)} recent/history scan scheduling passes")
        finally:
            native.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = python_url
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
