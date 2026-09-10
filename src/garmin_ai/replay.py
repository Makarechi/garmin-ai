"""Durable offline reprocessing of canonical raw revisions after parser upgrades."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, String, cast, func, or_, select, update
from sqlalchemy.orm import aliased

from garmin_ai.accounts import account_transaction
from garmin_ai.fit import store_fit
from garmin_ai.ingest import ingest
from garmin_ai.jobs import enqueue
from garmin_ai.models import (
    Activity,
    AppState,
    Insight,
    Job,
    MetricObservation,
    PendingQuestion,
    SourcePayload,
)
from garmin_ai.normalize import PARSER_VERSION, upsert
from garmin_ai.reconciliation import Replacement


def obsolete_completion(job):
    return (
        select(AppState.key)
        .where(
            AppState.key
            == func.concat(
                "replay:", job.payload["raw_ref"].astext, ":", job.payload["target_version"].astext
            ),
            AppState.value["status"].astext == "obsolete_target",
        )
        .exists()
    )


REPLAY_NOTICE = "Данные Garmin пересчитываются после изменения версии обработки. Анализ временно недоступен; это не означает отсутствие данных. Проверьте /status позже."


def canonical_source():
    # Older ingest versions retained a newer failed raw revision without moving
    # their success watermark. Give the newest such attempt a chance to replay.
    watermark = aliased(AppState)
    failed = aliased(SourcePayload)
    state_key = func.concat(
        "ingest:", SourcePayload.source, ":", SourcePayload.endpoint, ":", SourcePayload.source_key
    )
    latest_failed = (
        select(failed.id)
        .where(
            failed.source == SourcePayload.source,
            failed.endpoint == SourcePayload.endpoint,
            failed.source_key == SourcePayload.source_key,
            failed.status == "error",
            failed.fetched_at
            > cast(watermark.value["requested_at"].astext, DateTime(timezone=True)),
        )
        .order_by(failed.fetched_at.desc(), failed.id.desc())
        .limit(1)
        .correlate(SourcePayload, watermark)
        .scalar_subquery()
    )
    superseded_json = (
        select(watermark.key)
        .where(
            watermark.key == state_key,
            func.coalesce(
                cast(latest_failed, String), watermark.value["source_ref"].astext
            ).is_distinct_from(cast(SourcePayload.id, String)),
        )
        .correlate(SourcePayload)
        .exists()
    )
    superseded_fit = (
        select(Activity.id)
        .where(
            Activity.id == SourcePayload.source_key,
            Activity.fit_key.is_distinct_from(SourcePayload.archive_key),
        )
        .correlate(SourcePayload)
        .exists()
    )
    return or_(
        (SourcePayload.endpoint == "activity_fit") & ~superseded_fit,
        (SourcePayload.endpoint != "activity_fit") & ~superseded_json,
    )


def projection_mismatch():
    return (SourcePayload.parser_version != PARSER_VERSION) & canonical_source()


def replay_pending_condition():
    """Both upgrade and rollback require the current canonical projection version."""
    return select(SourcePayload.id).where(projection_mismatch()).correlate(None).exists()


def schedule_replay(session, now):
    binding = session.get(AppState, "account:garmin")
    if not binding:
        return
    if not session.scalar(select(func.pg_try_advisory_xact_lock(72104619))):
        return
    queued = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.kind == "raw_replay",
            Job.status.in_(["pending", "running"]),
            Job.payload["target_version"].as_integer() == PARSER_VERSION,
        )
    )
    budget = min(25, max(0, 100 - queued))
    if not budget:
        return
    # Repair success-like markers created by an earlier implementation without
    # projecting anything. Keep the original durable job identity on rollback.
    repaired = session.scalars(
        select(Job)
        .where(
            Job.kind == "raw_replay",
            Job.status.in_(["done", "failed"]),
            or_(
                Job.status == "done",
                select(SourcePayload.id)
                .where(
                    cast(SourcePayload.id, String) == Job.payload["raw_ref"].astext,
                    Job.payload["repair_parser_version"]
                    .as_integer()
                    .is_distinct_from(SourcePayload.parser_version),
                )
                .correlate(Job)
                .exists(),
            ),
            Job.payload["target_version"].as_integer() == PARSER_VERSION,
            or_(
                obsolete_completion(Job),
                select(SourcePayload.id)
                .where(
                    cast(SourcePayload.id, String) == Job.payload["raw_ref"].astext,
                    projection_mismatch(),
                )
                .correlate(Job)
                .exists(),
            ),
        )
        .order_by(Job.run_at, Job.id)
        .limit(budget)
        .with_for_update(skip_locked=True)
    ).all()
    for job in repaired:
        raw = session.get(SourcePayload, UUID(job.payload["raw_ref"]))
        job.payload = {**job.payload, "repair_parser_version": raw.parser_version if raw else None}
        job.status, job.attempts, job.run_at = "pending", 0, now
        job.completed_at = job.lease_until = job.lease_token = job.last_error = None
    budget -= len(repaired)
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
            SourcePayload.parser_version != PARSER_VERSION,
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
    if payload["target_version"] > PARSER_VERSION:
        raise ValueError("Replay requires a newer parser version")
    if payload["target_version"] < PARSER_VERSION:
        raise ValueError("Replay requires its matching parser version")
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
        if row.parser_version == PARSER_VERSION and row.status in {"normalized", "empty"}:
            return {"status": "unchanged", "source_ref": str(row.id)}
        state = session.get(AppState, f"fit-version:{row.source_key}")
        if state:
            at = datetime.fromisoformat(state.value["requested_at"])
        result = store_fit(session, archive, row.source_key, data, fetched_at=at)
    else:
        state = session.get(AppState, f"ingest:{row.source}:{row.endpoint}:{row.source_key}")
        if not session.scalar(
            select(SourcePayload.id).where(SourcePayload.id == row.id, canonical_source())
        ):
            return {"status": "superseded_revision"}
        if (
            state
            and state.value.get("source_ref") == str(row.id)
            and state.value.get("requested_at")
        ):
            # Repeated A retains its original raw fetched_at; the current watermark
            # carries the later A -> B -> A correction.
            at = datetime.fromisoformat(state.value["requested_at"])
        metadata = session.get(AppState, f"ingest-meta:{row.id}")
        timezone = metadata.value["timezone"] if metadata else None
        if timezone is None:
            zones = session.scalars(
                select(MetricObservation.timezone)
                .where(MetricObservation.source_ref == row.id)
                .distinct()
            ).all()
            if len(zones) == 1:
                timezone = zones[0]
            elif row.endpoint in {"daily", "body_battery", "hydration", "max_metrics"}:
                timezone = settings.timezone  # Date-keyed projections do not interpret wall time.
            elif row.endpoint == "activity" and session.get(Activity, row.source_key):
                timezone = session.get(Activity, row.source_key).timezone
            elif row.endpoint == "activities":
                entries = json.loads(data)
                if not isinstance(entries, list) or any(
                    not (item.get("timeZoneUnitDTO") or {}).get("timeZone")
                    and session.get(Activity, str(item.get("activityId"))) is None
                    for item in entries
                ):
                    raise ValueError("Historical activity timezone is unavailable")
                # Each item resolves its own source/existing timezone in normalize_activity.
                timezone = settings.timezone
            elif row.status in {"normalized", "partial"}:
                raise ValueError("Historical interpretation timezone is unavailable")
            else:
                timezone = settings.timezone  # No previous successful interpretation.
        result = ingest(
            session,
            archive,
            row.endpoint,
            row.source_key,
            json.loads(data),
            timezone,
            source=row.source,
            rebuild_projection=True,
            fetched_at=at,
            replacement=Replacement.restore(state.value.get("replacement")) if state else None,
        )
    if result["status"] not in {"error", "stale", "unchanged"}:
        session.execute(
            update(PendingQuestion)
            .where(PendingQuestion.kind == "context", PendingQuestion.status == "pending")
            .values(status="cancelled")
        )
        session.execute(
            update(Insight)
            .where(Insight.status.in_(["candidate", "accepted", "delivered", "uncertain"]))
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
        "ready": not bool(session.scalar(select(replay_pending_condition()))),
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
