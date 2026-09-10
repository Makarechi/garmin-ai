from datetime import UTC, date, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.archive import LocalArchive
from garmin_ai.models import (
    AppState,
    HealthDay,
    Measurement,
    MetricObservation,
    SourcePayload,
    TimelineInterval,
)
from garmin_ai.normalize import PARSER_VERSION, normalize, upsert
from garmin_ai.projection_history import load_history, previous_observations, record_application
from garmin_ai.reconciliation import Replacement, invalidate_insights, replace_interval


def ingest(
    session,
    archive: LocalArchive,
    endpoint: str,
    source_key: str,
    payload,
    timezone: str,
    source="garmin_connect",
    fetched_at=None,
    replacement: Replacement | None = None,
    rebuild_projection: bool = False,
    replay=False,
):
    fetched_at = fetched_at or datetime.now(UTC)
    if fetched_at.tzinfo is None:
        raise ValueError("Fetch timestamp must be timezone-aware")
    if replacement:
        replacement.validate(endpoint)
    contract = replacement.serialize() if replacement else None
    session.execute(select(func.pg_advisory_xact_lock(72104619)))
    logical_key = f"{source}:{endpoint}:{source_key}"
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(logical_key, 0))))
    archive_key = archive.put_json(payload)
    digest = archive_key.split("/")[-1].split(".")[0]
    stmt = (
        insert(SourcePayload)
        .values(
            source=source,
            endpoint=endpoint,
            source_key=source_key,
            payload_hash=digest,
            payload=payload,
            archive_key=archive_key,
            fetched_at=fetched_at,
        )
        .on_conflict_do_nothing(index_elements=["source", "endpoint", "source_key", "payload_hash"])
    )
    session.execute(stmt)
    raw = session.scalar(
        select(SourcePayload)
        .where(
            SourcePayload.source == source,
            SourcePayload.endpoint == endpoint,
            SourcePayload.source_key == source_key,
            SourcePayload.payload_hash == digest,
        )
        .with_for_update()
    )
    state_key = f"ingest:{source}:{endpoint}:{source_key}"
    state = session.get(AppState, state_key, populate_existing=True)
    previous_state = dict(state.value) if state else {}
    latest_attempt = previous_state.get("latest_attempt", {})
    preserve_attempt = bool(
        replay
        and previous_state.get("source_ref") == str(raw.id)
        and latest_attempt
        and latest_attempt.get("source_ref") != str(raw.id)
    )
    last_requested = latest_attempt.get("requested_at") or previous_state.get("requested_at")
    if (
        last_requested
        and fetched_at < datetime.fromisoformat(last_requested)
        and not preserve_attempt
    ):
        if raw.status == "pending":
            raw.status = "stale"
        return {"status": "stale", "source_ref": str(raw.id)}
    metadata_key = f"ingest-meta:{raw.id}"
    # A -> B -> A is a legitimate upstream correction, not an identical replay.
    unchanged = (
        state
        and state.value.get("hash") == digest
        and raw.parser_version == PARSER_VERSION
        and raw.status not in {"pending", "error"}
        and state.value.get("replacement") == contract
    )
    if not unchanged:
        upsert(session, AppState, dict(key=metadata_key, value={"timezone": timezone}), ["key"])
    shared_targets = endpoint in {"activity", "activities", "daily", "heart_rate", "body_battery"}
    if not unchanged or shared_targets:
        try:
            with session.begin_nested():
                if raw.parser_version != PARSER_VERSION:
                    clear_daily_projection(session, raw.id, source_key)
                    session.execute(
                        delete(TimelineInterval).where(
                            TimelineInterval.label == "sleep",
                            TimelineInterval.evidence["source_ref"].astext == str(raw.id),
                        )
                    )
                history = load_history(session, raw) if not unchanged else []
                if (rebuild_projection or raw.parser_version != PARSER_VERSION) and not unchanged:
                    restored = previous_observations(session, archive, raw, history)
                    session.execute(delete(Measurement).where(Measurement.source_ref == raw.id))
                    for observation in restored:
                        upsert(session, Measurement, observation, ["ts", "metric", "source"])
                if replacement and not unchanged:
                    replace_interval(session, source, endpoint, source_key, replacement)
                if raw.parser_version != PARSER_VERSION:
                    # The journal rebuild above already clears rejected owned samples
                    # and restores older overlapping partial observations atomically.
                    session.execute(
                        delete(MetricObservation).where(MetricObservation.source_ref == raw.id)
                    )
                session.info["fetch_time"] = fetched_at
                session.info["skip_samples"] = unchanged
                session.info["rebuilding_activity"] = (
                    replay and raw.parser_version != PARSER_VERSION
                )
                try:
                    raw.status = normalize(session, endpoint, source_key, payload, raw.id, timezone)
                finally:
                    session.info.pop("rebuilding_activity", None)
                raw.parser_version = PARSER_VERSION
                if not unchanged:
                    record_application(session, raw, history, timezone, fetched_at, contract)
                    invalidate_insights(session, endpoint, timezone)
        except Exception as exc:
            raw.status = "error"
            upsert(
                session,
                AppState,
                {
                    "key": metadata_key,
                    "value": {"timezone": timezone, "failed_parser_version": PARSER_VERSION},
                },
                ["key"],
            )
            attempt = (
                latest_attempt
                if preserve_attempt
                else {
                    "hash": digest,
                    "source_ref": str(raw.id),
                    "requested_at": fetched_at.isoformat(),
                    "parser_version": PARSER_VERSION,
                    "replacement": contract,
                    "completeness": "adapter_attested" if replacement else "unverified",
                }
            )
            upsert(
                session,
                AppState,
                {
                    "key": state_key,
                    "value": {**previous_state, "status": "error", "latest_attempt": attempt},
                },
                ["key"],
            )
            session.flush()
            return {"status": "error", "error_type": type(exc).__name__, "source_ref": str(raw.id)}
    if preserve_attempt:
        return {"status": "unchanged" if unchanged else raw.status, "source_ref": str(raw.id)}
    upsert(
        session,
        AppState,
        dict(
            key=state_key,
            value={
                "hash": digest,
                "source_ref": str(raw.id),
                "fetched_at": datetime.now(UTC).isoformat(),
                "requested_at": fetched_at.isoformat(),
                "status": raw.status,
                "replacement": contract,
                "completeness": "adapter_attested" if replacement else "unverified",
            },
        ),
        ["key"],
    )
    return {"status": "unchanged" if unchanged else raw.status, "source_ref": str(raw.id)}


def clear_daily_projection(session, ref, source_key):
    try:
        day = date.fromisoformat(source_key)
    except (TypeError, ValueError):
        return
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"health-day:{day}", 0)))
    )
    row = session.get(HealthDay, day, populate_existing=True)
    if row is None:
        return
    sources = dict(row.sources)
    for field in HealthDay.__table__.columns.keys():
        if field not in {"day", "sources", "updated_at"} and sources.get(f"field:{field}") == str(
            ref
        ):
            setattr(row, field, None)
            sources.pop(f"field:{field}", None)
            sources.pop(f"time:{field}", None)
    sources.pop(f"payload:{ref}", None)
    row.sources = sources
    session.flush()
