"""Durable offline reprocessing of canonical raw revisions after parser upgrades."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, case, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH, aggregate_order_by
from sqlalchemy.orm import aliased

from garmin_ai.accounts import account_transaction
from garmin_ai.fit import store_fit
from garmin_ai.ingest import ingest
from garmin_ai.jobs import enqueue
from garmin_ai.models import (
    Activity,
    ActivityPart,
    AppState,
    HealthDay,
    Insight,
    Job,
    Measurement,
    MetricObservation,
    PendingQuestion,
    SourcePayload,
    TimelineInterval,
)
from garmin_ai.normalize import PARSER_VERSION, upsert


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


def replay_generation(session):
    return session.scalar(select(AppState.value).where(AppState.key == "replay:generation"))


def invalidate_outputs(session):
    session.execute(delete(AppState).where(AppState.key == "analysis:conversation:pending"))
    session.execute(
        update(AppState)
        .where(AppState.key == "analysis:conversation")
        .values(value=AppState.value.op("||")({"turns": []}))
    )
    session.execute(
        update(PendingQuestion)
        .where(
            PendingQuestion.kind == "context",
            PendingQuestion.status.in_(["pending", "sending", "sent", "uncertain"]),
        )
        .values(status="cancelled")
    )
    session.execute(
        update(Insight)
        .where(Insight.status.in_(["candidate", "accepted", "delivered", "uncertain"]))
        .values(status="superseded")
    )
    upsert(session, AppState, {"key": "replay:generation", "value": {"id": str(uuid4())}}, ["key"])


def canonical_source():
    # Older ingest versions retained a newer failed raw revision without moving
    # their success watermark. Give the newest such attempt a chance to replay.
    watermark = aliased(AppState)
    failed = aliased(SourcePayload)
    attempted = aliased(AppState)
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
            or_(
                cast(failed.id, String) == watermark.value["latest_attempt"]["source_ref"].astext,
                failed.fetched_at
                > cast(watermark.value["requested_at"].astext, DateTime(timezone=True)),
                watermark.value["latest_attempt"]["source_ref"].astext.is_(None)
                & (
                    failed.fetched_at
                    == cast(watermark.value["requested_at"].astext, DateTime(timezone=True))
                ),
            ),
            ~select(attempted.key)
            .where(
                attempted.key == func.concat("ingest-meta:", cast(failed.id, String)),
                attempted.value["failed_parser_version"].as_integer() == PARSER_VERSION,
            )
            .correlate(failed)
            .exists(),
        )
        .order_by(failed.fetched_at.desc(), failed.id.desc())
        .limit(1)
        .correlate(SourcePayload, watermark)
        .scalar_subquery()
    )
    # Partial and empty responses can both retain older source-owned projections.
    page_entries = func.jsonb_array_elements(
        case(
            (func.jsonb_typeof(SourcePayload.payload) == "array", SourcePayload.payload),
            else_=func.jsonb_build_array(),
        )
    ).table_valued("value")
    retained_activity = (
        select(Activity.id)
        .select_from(Activity, page_entries)
        .where(cast(page_entries.c.value, JSONB)["activityId"].astext == Activity.id)
        .where(
            ~select(AppState.key)
            .where(
                AppState.key == func.concat("activity-version:", Activity.id),
                AppState.value["owners_initialized"].as_boolean().is_(True),
            )
            .correlate(Activity)
            .exists()
        )
        .correlate(SourcePayload)
        .exists()
    )
    legacy_parts = (
        select(func.jsonb_agg(aggregate_order_by(ActivityPart.payload, ActivityPart.sequence)))
        .where(
            ActivityPart.activity_id == SourcePayload.source_key,
            ActivityPart.kind == SourcePayload.endpoint,
        )
        .correlate(SourcePayload)
        .scalar_subquery()
    )
    retained_owner = or_(
        (SourcePayload.endpoint.startswith("activity_"))
        & (SourcePayload.parser_version > 0)
        & ~select(AppState.key)
        .where(
            AppState.key
            == func.concat(
                "activity-parts-owner:", SourcePayload.source_key, ":", SourcePayload.endpoint
            )
        )
        .correlate(SourcePayload)
        .exists()
        & (
            legacy_parts
            == case(
                (func.jsonb_typeof(SourcePayload.payload) == "array", SourcePayload.payload),
                else_=func.jsonb_build_array(SourcePayload.payload),
            )
        ),
        select(AppState.key)
        .where(
            or_(
                AppState.key.startswith("sample-owner:"),
                AppState.key.startswith("interval-owner:"),
                AppState.key.startswith("observation-owner:"),
                AppState.key.startswith("activity-parts-owner:"),
            ),
            AppState.value["source_ref"].astext == cast(SourcePayload.id, String),
        )
        .correlate(SourcePayload)
        .exists(),
        select(AppState.key)
        .where(
            AppState.key.startswith("activity-version:"),
            func.jsonb_path_exists(
                AppState.value["owners"],
                cast("$.* ? (@ == $ref)", JSONPATH),
                func.jsonb_build_object("ref", cast(SourcePayload.id, String)),
            ),
        )
        .correlate(SourcePayload)
        .exists(),
        SourcePayload.status.in_(["normalized", "partial", "error"])
        & (SourcePayload.parser_version > 0)
        & or_(
            (SourcePayload.endpoint == "activities") & retained_activity,
            (SourcePayload.endpoint == "activity")
            & select(Activity.id)
            .where(Activity.id == SourcePayload.source_key)
            .where(
                ~select(AppState.key)
                .where(
                    AppState.key == func.concat("activity-version:", Activity.id),
                    AppState.value["owners_initialized"].as_boolean().is_(True),
                )
                .correlate(Activity)
                .exists()
            )
            .correlate(SourcePayload)
            .exists(),
        ),
        select(Measurement.source_ref)
        .where(Measurement.source_ref == SourcePayload.id)
        .correlate(SourcePayload)
        .exists(),
        select(MetricObservation.source_ref)
        .where(MetricObservation.source_ref == SourcePayload.id)
        .correlate(SourcePayload)
        .exists(),
        select(TimelineInterval.id)
        .where(TimelineInterval.evidence["source_ref"].astext == cast(SourcePayload.id, String))
        .correlate(SourcePayload)
        .exists(),
        select(HealthDay.day)
        .where(
            or_(
                *(
                    HealthDay.sources[f"field:{column}"].astext == cast(SourcePayload.id, String)
                    for column in HealthDay.__table__.columns.keys()
                    if column not in {"day", "sources", "updated_at"}
                )
            )
        )
        .correlate(SourcePayload)
        .exists(),
    )
    superseded_json = (
        select(watermark.key)
        .where(
            watermark.key == state_key,
            ~retained_owner,
            func.coalesce(
                cast(latest_failed, String), watermark.value["source_ref"].astext
            ).is_distinct_from(cast(SourcePayload.id, String)),
        )
        .correlate(SourcePayload)
        .exists()
    )
    # FIT uses the last successfully parsed archive as its projection watermark.
    # A newer failed attempt becomes eligible only when the parser changes.
    canonical_fit = (
        select(Activity.id)
        .outerjoin(watermark, watermark.key == func.concat("fit-version:", Activity.id))
        .where(
            Activity.id == SourcePayload.source_key,
            or_(
                SourcePayload.id == latest_failed,
                (
                    SourcePayload.archive_key
                    == func.coalesce(Activity.details["parsed_fit_key"].astext, Activity.fit_key)
                ),
            ),
        )
        .correlate(SourcePayload)
        .exists()
    )
    return or_(
        (SourcePayload.endpoint == "activity_fit") & canonical_fit,
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
        .order_by(canonical_source().desc(), SourcePayload.fetched_at, SourcePayload.id)
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
    if not session.scalar(
        select(SourcePayload.id).where(SourcePayload.id == row.id, canonical_source())
    ):
        return {"status": "superseded_revision"}
    data = archive.read(row.archive_key)
    if hashlib.sha256(data).hexdigest() != row.payload_hash:
        raise ValueError("Archived source hash mismatch")
    at = row.fetched_at
    if row.endpoint == "activity_fit":
        if row.parser_version == PARSER_VERSION and row.status in {"normalized", "empty"}:
            return {"status": "unchanged", "source_ref": str(row.id)}
        state = session.get(AppState, f"fit-version:{row.source_key}")
        if state:
            attempt = state.value.get("latest_attempt", {})
            requested = (
                attempt.get("requested_at")
                if attempt.get("source_ref") == str(row.id)
                else state.value.get("requested_at")
                if state.value.get("source_ref") == str(row.id)
                else None
            )
            if requested:
                at = datetime.fromisoformat(requested)
        result = store_fit(session, archive, row.source_key, data, fetched_at=at, replay=True)
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
        latest_attempt = state.value.get("latest_attempt", {}) if state else {}
        if latest_attempt.get("source_ref") == str(row.id):
            at = datetime.fromisoformat(latest_attempt["requested_at"])
        metadata = session.get(AppState, f"ingest-meta:{row.id}")
        if (
            metadata
            and metadata.value.get("applied_at")
            and latest_attempt.get("source_ref") != str(row.id)
        ):
            at = max(at, datetime.fromisoformat(metadata.value["applied_at"]))
        if not metadata or not metadata.value.get("applied_at"):
            # Legacy content-addressed raws may have been installed repeatedly.
            # Field provenance retains the application clock after displacement.
            for sources in session.scalars(
                select(HealthDay.sources).where(
                    HealthDay.sources.op("@?")(cast(f'$.* ? (@ == "{row.id}")', JSONPATH))
                )
            ):
                for key, owner in sources.items():
                    if key.startswith("field:") and owner == str(row.id):
                        applied = sources.get("time:" + key.removeprefix("field:"))
                        if applied:
                            at = max(at, datetime.fromisoformat(applied))
            observed_at = session.scalar(
                select(func.max(MetricObservation.fetched_at)).where(
                    MetricObservation.source_ref == row.id
                )
            )
            if observed_at:
                at = max(at, observed_at)
        timezone = metadata.value.get("timezone") if metadata else None
        if timezone is None:
            latest_observation = session.scalar(
                select(func.max(MetricObservation.fetched_at)).where(
                    MetricObservation.source_ref == row.id
                )
            )
            zones = session.scalars(
                select(MetricObservation.timezone)
                .where(MetricObservation.source_ref == row.id)
                .where(MetricObservation.fetched_at == latest_observation)
                .distinct()
            ).all()
            if len(zones) == 1:
                timezone = zones[0]
            elif row.endpoint in {
                "daily",
                "body_battery",
                "hydration",
                "max_metrics",
                "sleep",
                "readiness",
            }:
                timezone = settings.timezone  # Date-keyed projections do not interpret wall time.
            elif row.endpoint == "steps" and not (
                any(bucket.get("startGMT") for bucket in json.loads(data))
                or session.scalar(
                    select(Measurement.ts).where(Measurement.source_ref == row.id).limit(1)
                )
            ):
                timezone = settings.timezone
            elif row.endpoint in {"hrv", "heart_rate", "stress", "respiration", "spo2"} and not (
                any(
                    any(
                        point.get("readingTimeGMT")
                        if isinstance(point, dict)
                        else (point[0] is not None if isinstance(point, list) and point else False)
                        for point in (json.loads(data).get(key) or [])
                    )
                    for key in {
                        "hrv": ("hrvReadings",),
                        "heart_rate": ("heartRateValues",),
                        "stress": ("stressValuesArray", "bodyBatteryValuesArray"),
                        "respiration": ("respirationValuesArray",),
                        "spo2": ("spO2HourlyAverages", "spO2ValuesArray"),
                    }[row.endpoint]
                )
                or session.scalar(
                    select(Measurement.ts).where(Measurement.source_ref == row.id).limit(1)
                )
            ):
                timezone = settings.timezone
            elif row.endpoint == "activity" and session.get(Activity, row.source_key):
                timezone = session.get(Activity, row.source_key).timezone
            elif row.endpoint == "activities":
                entries = json.loads(data)
                if not isinstance(entries, list) or (
                    row.parser_version > 0
                    and any(
                        not (item.get("timeZoneUnitDTO") or {}).get("timeZone")
                        and session.get(Activity, str(item.get("activityId"))) is None
                        for item in entries
                    )
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
            fetched_at=at,
            replay=True,
        )
    changed = result["status"] in {"normalized", "partial", "empty", "archived"}
    if changed:
        invalidate_outputs(session)
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
