"""Measure two real nightly or weekly reconciliation passes with synthetic Garmin replies."""

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sync_workload import (
    SyntheticReader,
    claim_history,
    configure_synthetic_owner,
    prepare_daily_fixture,
    verify_archive,
)

from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.garmin_contract import ENDPOINTS
from garmin_ai.jobs import finish
from garmin_ai.models import Job, SourcePayload
from garmin_ai.sync import run_garmin_job, schedule_sync


def clocks(days):
    first = datetime(2026, 10, 26 if days == 30 else 27, 3, tzinfo=UTC)
    interval = timedelta(days=7 if days == 30 else 1)
    return first, interval


def prepare_fixture(root, days):
    first, interval = clocks(days)
    last = (first + interval).date()
    prepare_daily_fixture(
        root, (last - timedelta(days=offset) for offset in range(days + interval.days))
    )
    (root / "global-devices.json").write_text(
        json.dumps({"synthetic": "devices"}), encoding="utf-8"
    )


def run(days, fixture=None):
    settings = Settings(_env_file=None, backfill_days=0, timezone="UTC")
    engine = make_engine(settings)
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("The reconcile case requires a disposable test database")
    archive = LocalArchive(settings.data_dir / "raw")
    reader = SyntheticReader(fixture)
    day_endpoints = sum(endpoint.scope == "day" for endpoint in ENDPOINTS)
    global_endpoints = sum(endpoint.scope == "global" for endpoint in ENDPOINTS)
    expected_pending = days * day_endpoints + global_endpoints
    first_day, interval = clocks(days)
    passes = []
    try:
        configure_synthetic_owner(engine, reader, archive)
        for now in (first_day, first_day + interval):
            started = time.monotonic()
            with transaction(engine) as session:
                schedule_sync(session, settings, now)
                # Calendar dedup persists even when the scheduler sees the same slot twice.
                schedule_sync(session, settings, now)
                pending = session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.kind == "garmin_endpoint", Job.status == "pending")
                )
            if pending != expected_pending:
                raise RuntimeError(
                    f"Expected {expected_pending} reconciliation jobs, got {pending}"
                )
            for _ in range(pending):
                identity, lease, payload = claim_history(engine)
                run_garmin_job(engine, reader, archive, settings, "garmin_endpoint", payload)
                with transaction(engine) as session:
                    finish(session, identity, lease)
            passes.append({"jobs": pending, "seconds": round(time.monotonic() - started, 3)})
        with transaction(engine) as session:
            sources = session.scalar(select(func.count()).select_from(SourcePayload))
        expected_sources = (days + interval.days) * day_endpoints + global_endpoints
        if sources != expected_sources:
            raise RuntimeError(f"Expected {expected_sources} unique source payloads, got {sources}")
        verified = verify_archive(engine, archive)
        if verified != sources:
            raise RuntimeError("The synthetic reconciliation archives are incomplete")
        return {
            "window_days": days,
            "passes": passes,
            "fetch_counts": dict(sorted(reader.calls.items())),
            "archive_objects_verified": verified,
            "unique_source_payloads": sources,
            "same_bytes_reused": sum(p["jobs"] for p in passes) - sources,
        }
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("days", type=int, choices=(7, 30))
    fixture = parser.add_mutually_exclusive_group()
    fixture.add_argument("--prepare-history-fixture", type=Path)
    fixture.add_argument("--history-fixture", type=Path)
    args = parser.parse_args()
    if args.prepare_history_fixture:
        prepare_fixture(args.prepare_history_fixture, args.days)
    else:
        print(json.dumps(run(args.days, args.history_fixture)))
