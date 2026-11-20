"""Drive the ordinary archived FIT worker queue with a generated Docker fixture."""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fit_workload import ACTIVITY, START, archive_input
from fitlimit_workload import zip_member
from sqlalchemy import func, select

from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.fit import MAX_FIT_BYTES
from garmin_ai.jobs import enqueue
from garmin_ai.models import (
    Activity,
    ActivityPart,
    AppState,
    ChannelBinding,
    InboundMessage,
    Job,
    Person,
    SourcePayload,
)
from garmin_ai.replay import PARSER_VERSION

ACCOUNT = "a" * 64


def prepare(engine, archive, size, with_controls, with_mixed_controls=False):
    if size == "developer":
        data, fit_size = archive_input(50_000, with_developer=True)
        records = 50_006
    else:
        fit_size = MAX_FIT_BYTES + (size == "over-limit")
        data, records = zip_member(fit_size)
    key = archive.put_bytes(data, "zip")
    digest = hashlib.sha256(data).hexdigest()
    with transaction(engine) as session:
        if with_mixed_controls:
            from analytics_workload import START as ANALYSIS_START
            from analytics_workload import seed, utc

            metric = seed(session)
            analysis = {
                "operation": "aggregate_metric",
                "metric_key": metric,
                "start": utc(ANALYSIS_START).isoformat(),
                "end": utc(ANALYSIS_START + timedelta(days=365)).isoformat(),
                "method": "distribution",
                "knowledge_cutoff": datetime.now(UTC).isoformat(),
            }
        if with_controls:
            owner = session.scalar(select(Person.id).where(Person.singleton))
            configured = session.scalar(
                select(ChannelBinding).where(
                    ChannelBinding.channel == "telegram",
                    ChannelBinding.channel_instance_id == "primary",
                    ChannelBinding.external_id == "42",
                )
            )
            if configured is None:
                session.add(
                    ChannelBinding(
                        owner_id=owner,
                        channel="telegram",
                        channel_instance_id="primary",
                        external_id="42",
                        confirmation_method="synthetic-fixture",
                    )
                )
            elif configured.owner_id != owner:
                raise RuntimeError(
                    "The synthetic Telegram binding does not belong to the fixture owner"
                )
        session.add_all(
            [
                AppState(key="account:garmin", value={"fingerprint": ACCOUNT}),
                Activity(
                    id=ACTIVITY,
                    start=START,
                    end=START + timedelta(minutes=1),
                    kind="running",
                    timezone="UTC",
                ),
            ]
        )
        source = SourcePayload(
            endpoint="activity_fit",
            source_key=ACTIVITY,
            payload_hash=digest,
            archive_key=key,
            fetched_at=START,
            status="error",
            parser_version=PARSER_VERSION - 1,
        )
        session.add(source)
        session.flush()
        session.add(
            AppState(
                key=f"fit-version:{ACTIVITY}",
                value={
                    "latest_attempt": {
                        "source_ref": str(source.id),
                        "requested_at": START.isoformat(),
                        "parser_version": PARSER_VERSION - 1,
                    }
                },
            )
        )
        metadata = {
            "raw_ref": str(source.id),
            "target_version": PARSER_VERSION,
            "account": ACCOUNT,
        }
        job = enqueue(
            session,
            "raw_replay",
            metadata,
            f"raw-replay:{source.id}:{PARSER_VERSION}",
            datetime.now(UTC),
        )
        mixed = {}
        if with_mixed_controls:
            payload = {"fixture": "synthetic archive replay"}
            replay_key = archive.put_json(payload)
            replay_source = SourcePayload(
                endpoint="body_battery_events",
                source_key="2026-10-25",
                payload_hash=hashlib.sha256(archive.read(replay_key)).hexdigest(),
                archive_key=replay_key,
                fetched_at=START,
                status="error",
                parser_version=PARSER_VERSION - 1,
            )
            session.add(replay_source)
            session.flush()
            replay_job = enqueue(
                session,
                "raw_replay",
                {
                    "raw_ref": str(replay_source.id),
                    "target_version": PARSER_VERSION,
                    "account": ACCOUNT,
                },
                f"raw-replay:{replay_source.id}:{PARSER_VERSION}",
                datetime.now(UTC) + timedelta(seconds=3),
            )
            mixed = {
                "analysis": analysis,
                "replay_source": str(replay_source.id),
                "replay_job": str(replay_job),
            }
        return {
            "source": str(source.id),
            "job": str(job),
            "records": records,
            "uncompressed_bytes": fit_size,
            "compressed_bytes": len(data),
            "archive_sha256": digest,
            "with_controls": with_controls,
            **mixed,
        }


def lease_snapshot(engine, job):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with transaction(engine) as session:
            row = session.get(Job, job)
            if row.status == "running":
                return {"active": True, "attempt": row.attempts}
            if row.status in {"done", "failed", "cancelled"}:
                break
        time.sleep(0.1)
    return {"active": False}


def wait(engine, fixture):
    deadline = time.monotonic() + 780
    previous = None
    renewals = 0
    job = UUID(fixture["job"])
    marker = Path("/fit-fixture/worker-exit")
    while time.monotonic() < deadline:
        with transaction(engine) as session:
            result = session.get(AppState, f"replay:{fixture['source']}:{PARSER_VERSION}")
            replay = (
                session.get(AppState, f"replay:{fixture['replay_source']}:{PARSER_VERSION}")
                if "replay_source" in fixture
                else None
            )
            if result is not None and ("replay_source" not in fixture or replay is not None):
                return {
                    "status": result.value["status"],
                    "rows": result.value.get("rows", 0),
                    "error_type": result.value.get("error_type"),
                    "observed_lease_renewals": renewals,
                }
            if fixture["with_controls"]:
                row = session.get(Job, job)
                if row.status == "running" and row.lease_until is not None:
                    current = (row.attempts, row.lease_until)
                    if (
                        previous is not None
                        and current[0] == previous[0]
                        and current[1] > previous[1]
                    ):
                        renewals += 1
                    previous = current
        if marker.exists():
            return {
                "worker_exit_code": int(marker.read_text()),
                "observed_lease_renewals": renewals,
            }
        time.sleep(0.2)
    raise RuntimeError("The synthetic replay job did not publish an outcome")


def verify(engine, archive, fixture):
    digest = hashlib.sha256()
    with transaction(engine) as session:
        source = session.get(SourcePayload, UUID(fixture["source"]))
        if source.payload_hash != fixture["archive_sha256"]:
            raise RuntimeError("Stored archive identity changed")
        if hashlib.sha256(archive.read(source.archive_key)).hexdigest() != source.payload_hash:
            raise RuntimeError("Stored archive contents changed")
        count = 0
        rows = select(ActivityPart).where(ActivityPart.activity_id == ACTIVITY)
        for row in session.scalars(
            rows.order_by(ActivityPart.sequence).execution_options(yield_per=250)
        ):
            if row.sequence != count:
                raise RuntimeError("Stored FIT sequence is incomplete")
            count += 1
            encoded = json.dumps(
                [row.kind, row.sequence, row.payload],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            digest.update(encoded.encode() + b"\n")
        job = session.get(Job, UUID(fixture["job"]))
        controls = {}
        if fixture["with_controls"]:
            pause = session.get(AppState, "proactive:enabled")
            telegram_count = session.scalar(
                select(func.count())
                .select_from(InboundMessage)
                .where(InboundMessage.channel == "telegram")
            )
            web_count = session.scalar(
                select(func.count())
                .select_from(InboundMessage)
                .where(InboundMessage.channel == "web", InboundMessage.normalized_text == "/pause")
            )
            controls = {
                "owner_pause_persisted": pause is not None
                and pause.value.get("enabled") is False
                and web_count == 1,
                "telegram_ingress_persisted": telegram_count == 1,
            }
        if "replay_source" in fixture:
            replay = session.get(SourcePayload, UUID(fixture["replay_source"]))
            outcome = session.get(AppState, f"replay:{replay.id}:{PARSER_VERSION}")
            replay_job = session.get(Job, UUID(fixture["replay_job"]))
            controls["ordinary_replay_completed"] = (
                replay.parser_version == PARSER_VERSION
                and outcome.value["status"] == "archived"
                and replay_job.status == "done"
                and replay_job.attempts == 1
                and hashlib.sha256(archive.read(replay.archive_key)).hexdigest()
                == replay.payload_hash
            )

        return {
            **controls,
            "stored_rows": count,
            "projection_sha256": digest.hexdigest(),
            "source_status": source.status,
            "source_parser_version": source.parser_version,
            "job_status": job.status,
            "fit_pointer_published": session.get(Activity, ACTIVITY).fit_key == source.archive_key,
        }


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("mode", choices=("prepare", "running", "wait", "verify"))
    args.add_argument("value")
    args.add_argument("--with-controls", action="store_true")
    args.add_argument("--with-mixed-controls", action="store_true")
    options = args.parse_args()
    settings = Settings(_env_file=None)
    engine = make_engine(settings)
    if engine.url.database != "garmin_resource_test" or engine.url.query:
        raise ValueError("The queue probe requires the isolated resource database")
    if settings.data_dir != Path("/fit-fixture"):
        raise ValueError("The queue probe requires the dedicated synthetic archive")
    try:
        archive = LocalArchive(settings.data_dir / "raw")
        if options.mode == "prepare":
            result = prepare(
                engine, archive, options.value, options.with_controls, options.with_mixed_controls
            )
        elif options.mode == "running":
            result = lease_snapshot(engine, UUID(options.value))
        elif options.mode == "wait":
            result = wait(engine, json.loads(options.value))
        else:
            result = verify(engine, archive, json.loads(options.value))
        print(json.dumps(result))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
