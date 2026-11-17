"""Compare offline replay queue admission and repair on isolated stores."""

import argparse
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from check_replay_selection import ACCOUNT, BASE
from check_replay_selection import seed as seed_sources
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.models import AppState, Job
from garmin_ai.replay import schedule_replay

CALLS = 5


def seed(session):
    seed_sources(session)
    for identity, raw_ref, status in [
        (800, 1, "done"),
        (900, 999, "failed"),
    ]:
        session.add(
            Job(
                id=UUID(int=identity),
                kind="raw_replay",
                dedup_key=f"raw-replay:{UUID(int=raw_ref)}:9",
                payload={
                    "raw_ref": str(UUID(int=raw_ref)),
                    "target_version": 9,
                    "account": ACCOUNT,
                    "backfill": True,
                },
                run_at=BASE,
                status=status,
                attempts=8,
                completed_at=BASE,
                last_error="SyntheticObsolete",
            )
        )
    session.add(AppState(key=f"replay:{UUID(int=999)}:9", value={"status": "obsolete_target"}))


def snapshot(engine):
    with Session(engine) as session:
        jobs = [
            {
                "dedup_key": job.dedup_key,
                "run_at": job.run_at.isoformat(),
                "status": job.status,
                "attempts": job.attempts,
                "payload": job.payload,
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                "last_error": job.last_error,
            }
            for job in session.scalars(
                select(Job).where(Job.kind == "raw_replay").order_by(Job.dedup_key)
            )
        ]
        cursors = {
            row.key: row.value
            for row in session.scalars(
                select(AppState).where(
                    AppState.key.in_(["replay:source_scan_cursor", "replay:repair_scan_cursor"])
                )
            )
        }
        return {"jobs": jobs, "cursors": cursors}


def write_python(engine):
    with Session(engine) as session, session.begin():
        seed(session)
    results = []
    for index in range(CALLS):
        with Session(engine) as session, session.begin():
            total = schedule_replay(session, BASE + timedelta(seconds=1000 + index))
            results.append([total, session.info.get("replay_scan_incomplete", False)])
    return results


def run(binary):
    source = os.environ["GA_TEST_DATABASE_URL"]
    rust_target = os.environ["GA_TEST_RUST_DATABASE_URL"]
    if rust_target == source:
        raise ValueError("Use two distinct disposable databases")
    python = isolated_engine()
    try:
        output = write_python(python)
        os.environ["GA_TEST_DATABASE_URL"] = rust_target
        rust = isolated_engine()
        try:
            with Session(rust) as session, session.begin():
                seed(session)
            subprocess.run(
                [binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_REPLAY_QUEUE_OUTCOMES": json.dumps(output)},
            )
            actual = snapshot(rust)
            expected = snapshot(python)
            assert actual["cursors"] == expected["cursors"]
            assert len(actual["jobs"]) == len(expected["jobs"]) == 100
            for left, right in zip(actual["jobs"], expected["jobs"], strict=True):
                assert left == right, (left, right)
            print("Replay queue capacity, repaired jobs and saved cursors match pinned Python")
        finally:
            rust.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = source
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
