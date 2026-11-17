"""Measure offline replay of a synthetic archive and one deliberately corrupted input."""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sync_workload import DAY, SyntheticReader, configure_synthetic_owner

from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.garmin_contract import ENDPOINTS
from garmin_ai.jobs import claim, finish
from garmin_ai.models import HealthDay, Measurement, SourcePayload
from garmin_ai.replay import replay_status, run_replay, schedule_replay
from garmin_ai.sync import run_garmin_job


def projection_digest(session):
    snapshot = {
        "health_days": [
            [str(day), steps, sleep, hrv]
            for day, steps, sleep, hrv in session.execute(
                select(
                    HealthDay.day, HealthDay.steps, HealthDay.sleep_score, HealthDay.hrv_nightly_avg
                ).order_by(HealthDay.day)
            )
        ],
        "measurements": [
            [ts.isoformat(), metric, value, unit]
            for ts, metric, value, unit in session.execute(
                select(
                    Measurement.ts, Measurement.metric, Measurement.value, Measurement.unit
                ).order_by(Measurement.ts, Measurement.metric, Measurement.source)
            )
        ],
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def seed(engine, archive, settings, days):
    reader = SyntheticReader()
    configure_synthetic_owner(engine, reader, archive)
    endpoints = [endpoint.name for endpoint in ENDPOINTS if endpoint.scope == "day"]
    for offset in range(days):
        day = (DAY - timedelta(days=offset)).isoformat()
        for endpoint in endpoints:
            run_garmin_job(
                engine,
                reader,
                archive,
                settings,
                "garmin_endpoint",
                {"endpoint": endpoint, "key": day},
            )
    with transaction(engine) as session:
        rows = list(session.scalars(select(SourcePayload)))
        if len(rows) != days * len(endpoints):
            raise RuntimeError("Replay seed did not commit every input")
        damaged = next(
            row
            for row in rows
            if row.endpoint == "training_status" and row.source_key == DAY.isoformat()
        )
        corrupt_key = damaged.archive_key
        damaged_id = str(damaged.id)
        for row in rows:
            row.parser_version = 0
        baseline = projection_digest(session)
    (archive.root / corrupt_key).write_bytes(b"deliberately damaged benchmark archive")
    return len(rows), damaged_id, baseline


def run(days):
    settings = Settings(_env_file=None, timezone="UTC")
    engine = make_engine(settings)
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("The replay workload requires a dedicated synthetic test database")
    archive = LocalArchive(settings.data_dir / "raw")
    try:
        count, damaged_id, baseline = seed(engine, archive, settings, days)
        start = time.monotonic()
        succeeded, failed = set(), set()
        failed_attempts = 0
        for _ in range(count):
            with transaction(engine) as session:
                schedule_replay(session, datetime.now(UTC))
            for _ in range(25):
                if len(succeeded | failed) == count:
                    break
                with transaction(engine) as session:
                    job = claim(session, kinds=["raw_replay"])
                    if job is None:
                        break
                    identity, lease, payload = job.id, job.lease_token, dict(job.payload)
                ref = payload["raw_ref"]
                try:
                    run_replay(engine, archive, settings, payload)
                except RuntimeError as error:
                    if ref != damaged_id or "Offline replay" not in str(error):
                        raise
                    failed.add(ref)
                    failed_attempts += 1
                    with transaction(engine) as session:
                        finish(
                            session, identity, lease, error_type="ValueError", terminal_error=True
                        )
                else:
                    succeeded.add(ref)
                    with transaction(engine) as session:
                        finish(session, identity, lease)
            if len(succeeded | failed) == count:
                break
        else:
            raise RuntimeError("Replay planner did not reach all synthetic source objects")
        elapsed = time.monotonic() - start
        with transaction(engine) as session:
            final_digest = projection_digest(session)
            status = replay_status(session)["outcomes"]
        if failed != {damaged_id} or baseline != final_digest or status.get("error") != 1:
            raise RuntimeError(
                f"Replay had {len(failed)} corrupt objects, projection preserved={baseline == final_digest}, outcomes={status}"
            )
        return {
            "history_days": days,
            "archive_objects": count,
            "successful_replays": len(succeeded),
            "corrupt_objects_rejected": len(failed),
            "corrupt_attempts": failed_attempts,
            "projection_sha256": final_digest,
            "outcomes": status,
            "replay_seconds": round(elapsed, 3),
        }
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("days", type=int, choices=(7, 30))
    options = parser.parse_args()
    print(json.dumps(run(options.days)))
