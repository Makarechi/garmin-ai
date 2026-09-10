"""Durable offline reprocessing of canonical raw revisions after parser upgrades."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import String, cast, func, select, update

from garmin_ai.accounts import account_transaction
from garmin_ai.fit import store_fit
from garmin_ai.ingest import ingest
from garmin_ai.jobs import enqueue
from garmin_ai.models import Activity, AppState, Insight, Job, SourcePayload
from garmin_ai.normalize import PARSER_VERSION, upsert


def schedule_replay(session, now):
    binding = session.get(AppState, "account:garmin")
    if not binding:
        return
    session.execute(select(func.pg_advisory_xact_lock(72104619)))
    queued = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == "raw_replay", Job.status.in_(["pending", "running"]))
    )
    budget = min(25, max(0, 100 - queued))
    if not budget:
        return
    planned = (
        select(Job.id)
        .where(
            Job.kind == "raw_replay",
            Job.dedup_key
            == func.concat("raw-replay:", cast(SourcePayload.id, String), f":{PARSER_VERSION}"),
        )
        .exists()
    )
    for identity in session.scalars(
        select(SourcePayload.id)
        .where(
            SourcePayload.parser_version < PARSER_VERSION,
            ~planned,
        )
        .order_by(SourcePayload.fetched_at, SourcePayload.id)
        .limit(budget)
    ):
        enqueue(
            session,
            "raw_replay",
            {
                "raw_ref": str(identity),
                "target_version": PARSER_VERSION,
                "account": binding.value["fingerprint"],
                "backfill": True,
            },
            f"raw-replay:{identity}:{PARSER_VERSION}",
            now,
        )


def replay_source(session, archive, settings, payload):
    if payload["target_version"] != PARSER_VERSION:
        return {"status": "obsolete_target"}
    session.execute(select(func.pg_advisory_xact_lock(72104619)))
    row = session.get(SourcePayload, UUID(payload["raw_ref"]), populate_existing=True)
    if row is None:
        return {"status": "source_removed"}
    data = archive.read(row.archive_key)
    if hashlib.sha256(data).hexdigest() != row.payload_hash:
        raise ValueError("Archived source hash mismatch")
    at = row.fetched_at
    if row.endpoint == "activity_fit":
        activity = session.get(Activity, row.source_key)
        if activity and activity.fit_key != row.archive_key:
            return {"status": "superseded_revision"}
        state = session.get(AppState, f"fit-version:{row.source_key}")
        if state:
            at = datetime.fromisoformat(state.value["requested_at"])
        result = store_fit(session, archive, row.source_key, data, fetched_at=at)
    else:
        state = session.get(AppState, f"ingest:{row.source}:{row.endpoint}:{row.source_key}")
        if state and state.value.get("source_ref") != str(row.id):
            return {"status": "superseded_revision"}
        if state and state.value.get("requested_at"):
            # Repeated A retains its original raw fetched_at; the current watermark
            # carries the later A -> B -> A correction.
            at = datetime.fromisoformat(state.value["requested_at"])
        result = ingest(
            session,
            archive,
            row.endpoint,
            row.source_key,
            json.loads(data),
            settings.timezone,
            source=row.source,
            fetched_at=at,
        )
    if result["status"] not in {"error", "stale"}:
        session.execute(
            update(Insight)
            .where(Insight.status.in_(["candidate", "accepted", "delivered"]))
            .values(status="superseded")
        )
    return result


def run_replay(engine, archive, settings, payload):
    with account_transaction(engine, payload["account"], archive_root=archive.root) as session:
        try:
            with session.begin_nested():
                result = replay_source(session, archive, settings, payload)
        except Exception as exc:
            result = {"status": "error", "error_type": type(exc).__name__}
        upsert(
            session,
            AppState,
            {
                "key": f"replay:{payload['raw_ref']}:{payload['target_version']}",
                "value": {
                    **result,
                    "at": datetime.now(UTC).isoformat(),
                    "target_version": payload["target_version"],
                },
            },
            ["key"],
        )
    if result["status"] == "error":
        raise RuntimeError("Offline replay failed; inspect technical replay status")
    return result


def replay_status(session):
    outcome = AppState.value["status"].as_string()
    return {
        "target_parser_version": PARSER_VERSION,
        "jobs": dict(
            session.execute(
                select(Job.status, func.count())
                .where(
                    Job.kind == "raw_replay",
                    Job.payload["target_version"].as_integer() == PARSER_VERSION,
                )
                .group_by(Job.status)
            ).all()
        ),
        "outcomes": dict(
            session.execute(
                select(outcome, func.count())
                .where(
                    AppState.key.startswith("replay:"),
                    AppState.value["target_version"].as_integer() == PARSER_VERSION,
                )
                .group_by(outcome)
            ).all()
        ),
    }
