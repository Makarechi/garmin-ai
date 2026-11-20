"""Stage and verify a synthetic offline replay through the ordinary deployment workers."""

import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

from replay_workload import projection_digest, seed
from sqlalchemy import func, select

from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.models import AppState, Job, SourcePayload
from garmin_ai.replay import PARSER_VERSION, replay_status


def prepare(engine, archive, settings, days):
    count, damaged, before_projection = seed(engine, archive, settings, days)
    with transaction(engine) as session:
        boundary = session.scalar(select(func.clock_timestamp()))
    return {
        "history_days": days,
        "archive_objects": count,
        "damaged_id": damaged,
        "before_projection": before_projection,
        "observed_start": boundary.isoformat(),
    }


def wait(engine, fixture, *, worker=None):
    deadline = time.monotonic() + 1800
    count = fixture["archive_objects"]
    while time.monotonic() < deadline:
        if worker is not None and worker.poll() is not None:
            raise RuntimeError(f"Replay worker exited with status {worker.returncode}")
        with transaction(engine) as session:
            statuses = replay_status(session)
            outcomes = statuses["outcomes"]
            jobs = statuses["jobs"]
            failure = session.get(AppState, f"replay:{fixture['damaged_id']}:{PARSER_VERSION}")
            if (
                sum(outcomes.values()) == count
                and jobs.get("done") == count - 1
                and failure is not None
                and failure.value.get("status") == "error"
            ):
                mark = session.scalar(
                    select(func.max(Job.completed_at)).where(
                        Job.kind == "raw_replay", Job.status == "done"
                    )
                )
                start = datetime.fromisoformat(fixture["observed_start"])
                failed_at = datetime.fromisoformat(failure.value["at"])
                duration = (max(mark, failed_at) - start).total_seconds()
                if duration <= 0:
                    raise RuntimeError("Replay completion preceded its prepared archive")
                return {
                    "target_parser_version": statuses["target_parser_version"],
                    "outcomes": outcomes,
                    "successfully_replayed": jobs["done"],
                    "ready_with_corrupt_input": statuses["ready"],
                    "queue_and_startup_seconds": duration,
                }
        time.sleep(0.2)
    raise RuntimeError("Offline replay did not publish all expected outcomes")


def verify(engine, archive, fixture):
    broken = UUID(fixture["damaged_id"])
    saved = hashlib.sha256()
    damaged = 0
    checked = 0
    with transaction(engine) as session:
        final_projection = projection_digest(session)
        if final_projection != fixture["before_projection"]:
            raise RuntimeError("Offline replay changed the input projection unexpectedly")
        rows = session.scalars(
            select(SourcePayload)
            .order_by(SourcePayload.source, SourcePayload.endpoint, SourcePayload.source_key)
            .execution_options(yield_per=100)
        )
        for row in rows:
            actual = hashlib.sha256(archive.read(row.archive_key)).hexdigest()
            if row.id == broken:
                if (
                    actual != hashlib.sha256(b"deliberately damaged benchmark archive").hexdigest()
                    or actual == row.payload_hash
                    or row.parser_version == PARSER_VERSION
                ):
                    raise RuntimeError("The corrupt input did not remain quarantined for repair")
                damaged += 1
            else:
                if actual != row.payload_hash or row.parser_version != PARSER_VERSION:
                    raise RuntimeError("Offline replay lost or failed to upgrade a valid input")
                checked += 1
            line = (
                row.source,
                row.endpoint,
                row.source_key,
                row.payload_hash,
                row.archive_key,
                row.parser_version,
                row.status,
                actual,
            )
            saved.update((json.dumps(line, ensure_ascii=True) + "\n").encode())
        result_status = replay_status(session)
    if damaged != 1 or checked != fixture["archive_objects"] - 1:
        raise RuntimeError("Offline replay did not preserve the complete archive")
    if result_status["ready"] or result_status["outcomes"].get("error") != 1:
        raise RuntimeError("Replay advertised complete projections after detecting corruption")
    return {
        "archive_rows_sha256": saved.hexdigest(),
        "projection_sha256": final_projection,
        "valid_archives_verified": checked,
        "corrupt_archives_preserved": damaged,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "wait", "verify"))
    parser.add_argument("input")
    args = parser.parse_args()
    settings = Settings(_env_file=None, timezone="UTC")
    engine = make_engine(settings)
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("Paired replay requires a dedicated synthetic test database")
    archive = LocalArchive(Path(settings.data_dir) / "raw")
    try:
        if args.phase == "prepare":
            days = int(args.input)
            if days not in (7, 30):
                parser.error("The paired replay supports 7 or 30 source days")
            result = prepare(engine, archive, settings, days)
        elif args.phase == "wait":
            result = wait(engine, json.loads(args.input))
        else:
            result = verify(engine, archive, json.loads(args.input))
        print(json.dumps(result))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
