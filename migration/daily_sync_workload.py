"""Measure the ordinary evening Garmin queue with a shared synthetic day and activity."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sync_workload import (
    ACTIVITY_ID,
    DAY,
    SyntheticReader,
    configure_synthetic_owner,
    prepare_daily_fixture,
    verify_archive,
)

from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.garmin_contract import ENDPOINTS
from garmin_ai.jobs import GARMIN_JOB_KINDS, claim, finish
from garmin_ai.models import Job
from garmin_ai.sync import run_garmin_job, schedule_sync

EVENING = datetime.combine(DAY, datetime.min.time(), tzinfo=UTC).replace(hour=18)
EXPECTED_CALLS = len(ENDPOINTS) + 2
EXPECTED_FOLLOWUPS = sum(endpoint.scope == "activity" for endpoint in ENDPOINTS)


def prepare_fixture(root):
    prepare_daily_fixture(root, [DAY])
    source = SyntheticReader()
    activity_endpoints = [endpoint for endpoint in ENDPOINTS if endpoint.scope == "activity"]
    (root / "activity-endpoints.txt").write_text(
        "".join(endpoint.name + "\n" for endpoint in activity_endpoints), encoding="ascii"
    )
    (root / "activity-id.txt").write_text(ACTIVITY_ID, encoding="ascii")
    for endpoint in activity_endpoints:
        (root / f"{ACTIVITY_ID}-{endpoint.name}.json").write_text(
            json.dumps(source.fetch(endpoint, activity_id=ACTIVITY_ID)), encoding="utf-8"
        )
    (root / "global-devices.json").write_text(
        json.dumps({"synthetic": "devices"}), encoding="utf-8"
    )
    (root / "page:0-activities.json").write_text(
        json.dumps(source.call("get_activities", 0, 100)), encoding="utf-8"
    )
    (root / "activity.fit.zip").write_bytes(source.call("download_activity", ACTIVITY_ID))


class DailySource(SyntheticReader):
    def fetch(self, endpoint, *, day=None, activity_id=None):
        key = {"day": day, "activity": activity_id, "global": "global"}[endpoint.scope]
        self.calls[endpoint.name] += 1
        return json.loads((self.fixture / f"{key}-{endpoint.name}.json").read_bytes())

    def call(self, method, *args, **kwargs):
        if method == "get_activities" and args == (0, 100):
            self.calls["activities"] += 1
            return json.loads((self.fixture / "page:0-activities.json").read_bytes())
        if method == "download_activity" and args == (ACTIVITY_ID,):
            self.calls["activity_fit"] += 1
            return (self.fixture / "activity.fit.zip").read_bytes()
        raise ValueError("Unexpected request in the synthetic evening workload")


def run(fixture):
    settings = Settings(_env_file=None, backfill_days=0, timezone="UTC")
    engine = make_engine(settings)
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("The evening workload requires a dedicated synthetic test database")
    archive = LocalArchive(settings.data_dir / "raw")
    reader = DailySource(fixture)
    try:
        configure_synthetic_owner(engine, reader, archive)
        started = time.monotonic()
        with transaction(engine) as session:
            schedule_sync(session, settings, EVENING)
        for _ in range(EXPECTED_CALLS):
            deadline = time.monotonic() + 10
            while True:
                with transaction(engine) as session:
                    job = claim(session, kinds=sorted(GARMIN_JOB_KINDS), backups_enabled=False)
                    if job is not None:
                        identity, lease, kind, payload = (
                            job.id,
                            job.lease_token,
                            job.kind,
                            dict(job.payload),
                        )
                        break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Evening jobs stopped making progress")
                time.sleep(0.05)
            run_garmin_job(engine, reader, archive, settings, kind, payload)
            with transaction(engine) as session:
                finish(session, identity, lease)
        duration = time.monotonic() - started
        with transaction(engine) as session:
            counts = dict(
                session.execute(
                    select(Job.status, func.count())
                    .where(Job.kind.in_(GARMIN_JOB_KINDS))
                    .group_by(Job.status)
                ).all()
            )
        assert counts == {"done": EXPECTED_CALLS, "pending": EXPECTED_FOLLOWUPS}, counts
        assert len(reader.calls) == EXPECTED_CALLS and set(reader.calls.values()) == {1}
        verified = verify_archive(engine, archive)
        assert verified == EXPECTED_CALLS
        return {
            "days": 1,
            "duration_seconds": duration,
            "fetch_counts": dict(sorted(reader.calls.items())),
            "jobs_done": counts["done"],
            "delayed_followups": counts["pending"],
            "archive_objects_verified": verified,
        }
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("days", type=int, choices=(1,))
    fixture = parser.add_mutually_exclusive_group(required=True)
    fixture.add_argument("--prepare-history-fixture", type=Path)
    fixture.add_argument("--history-fixture", type=Path)
    args = parser.parse_args()
    if args.prepare_history_fixture:
        prepare_fixture(args.prepare_history_fixture)
    else:
        print(json.dumps(run(args.history_fixture)))
