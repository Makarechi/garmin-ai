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
from garmin_ai.projection_changes import execute_projection
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
    # Legacy empty responses moved the success pointer despite retaining data.
    if replay and previous_state.get("status") == "empty" and not latest_attempt:
        latest_attempt = {
            key: previous_state[key]
            for key in ("source_ref", "hash", "requested_at", "status")
            if key in previous_state
        }
    preserve_attempt = bool(
        replay
        and latest_attempt
        and latest_attempt.get("source_ref") != str(raw.id)
        and latest_attempt.get("requested_at")
        and datetime.fromisoformat(latest_attempt["requested_at"]) >= fetched_at
    )
    last_requested = latest_attempt.get("requested_at") or previous_state.get("requested_at")
    retained_replay = bool(
        replay
        and previous_state.get("source_ref") != str(raw.id)
        and latest_attempt.get("source_ref") != str(raw.id)
        and previous_state.get("requested_at")
        and datetime.fromisoformat(previous_state["requested_at"]) >= fetched_at
    )
    if (
        last_requested
        and fetched_at < datetime.fromisoformat(last_requested)
        and not preserve_attempt
        and not retained_replay
    ):
        if raw.status == "pending":
            raw.status = "stale"
        return {"status": "stale", "source_ref": str(raw.id)}
    metadata_key = f"ingest-meta:{raw.id}"
    metadata = session.get(AppState, metadata_key, populate_existing=True)
    previous_metadata = dict(metadata.value) if metadata else {}
    # A -> B -> A is a legitimate upstream correction, not an identical replay.
    parser_transition = (
        raw.parser_version > 0 or raw.status == "error"
    ) and raw.parser_version != PARSER_VERSION
    unchanged = (
        state
        and state.value.get("hash") == digest
        and raw.parser_version == PARSER_VERSION
        and raw.status not in {"pending", "error"}
        and state.value.get("replacement") == contract
    )
    if not unchanged:
        upsert(
            session,
            AppState,
            dict(key=metadata_key, value={**previous_metadata, "timezone": timezone}),
            ["key"],
        )
    session.info["projection_changed"] = False
    shared_targets = endpoint in {"activity", "activities", "daily", "heart_rate", "body_battery"}
    owned_samples = (
        set(
            session.execute(
                select(Measurement.ts, Measurement.metric, Measurement.source).where(
                    Measurement.source_ref == raw.id
                )
            ).all()
        )
        if retained_replay
        else None
    )
    owned_intervals = (
        set(
            session.scalars(
                select(TimelineInterval.id).where(
                    TimelineInterval.evidence["source_ref"].astext == str(raw.id)
                )
            )
        )
        if retained_replay
        else None
    )
    if not unchanged or shared_targets:
        try:
            with session.begin_nested():
                if replacement and not unchanged and not retained_replay:
                    from garmin_ai.reconciliation import interval_projection

                    session.info["replacement_scope"] = (source, replacement)
                    session.info["replacement_snapshot"] = interval_projection(
                        session, source, replacement
                    )
                if raw.parser_version != PARSER_VERSION:
                    for sample in session.execute(
                        select(Measurement.ts, Measurement.metric, Measurement.source).where(
                            Measurement.source_ref == raw.id
                        )
                    ):
                        upsert(
                            session,
                            AppState,
                            dict(
                                key=f"sample-owner:{sample.ts.isoformat()}:{sample.metric}:{sample.source}",
                                value={
                                    "source_ref": str(raw.id),
                                    "metric": sample.metric,
                                    "source": sample.source,
                                    "ts": sample.ts.isoformat(),
                                },
                            ),
                            ["key"],
                        )
                    for interval_id in session.scalars(
                        select(TimelineInterval.id).where(
                            TimelineInterval.label == "sleep",
                            TimelineInterval.evidence["source_ref"].astext == str(raw.id),
                        )
                    ):
                        upsert(
                            session,
                            AppState,
                            dict(
                                key=f"interval-owner:{interval_id}",
                                value={"source_ref": str(raw.id)},
                            ),
                            ["key"],
                        )
                    from garmin_ai.temporal import preserve_observation_owners

                    preserve_observation_owners(session, raw.id)
                    clear_daily_projection(session, raw.id, source_key)
                    execute_projection(
                        session,
                        delete(TimelineInterval).where(
                            TimelineInterval.label == "sleep",
                            TimelineInterval.evidence["source_ref"].astext == str(raw.id),
                        ),
                    )
                history = load_history(session, raw) if not unchanged else []
                if (rebuild_projection or raw.parser_version != PARSER_VERSION) and not unchanged:
                    restored = previous_observations(session, archive, raw, history)
                    execute_projection(
                        session, delete(Measurement).where(Measurement.source_ref == raw.id)
                    )
                    for observation in restored:
                        upsert(session, Measurement, observation, ["ts", "metric", "source"])
                if replacement and not unchanged and not retained_replay:
                    replace_interval(session, source, endpoint, source_key, replacement)
                if raw.parser_version != PARSER_VERSION:
                    # The journal rebuild above already clears rejected owned samples
                    # and restores older overlapping partial observations atomically.
                    execute_projection(
                        session,
                        delete(MetricObservation).where(MetricObservation.source_ref == raw.id),
                    )
                session.info["fetch_time"] = fetched_at
                session.info["skip_samples"] = unchanged
                session.info["replaying_projection"] = replay
                session.info["replay_preceding_source"] = (
                    previous_state.get("source_ref")
                    if replay and latest_attempt.get("source_ref") == str(raw.id)
                    else None
                )
                session.info["rebuilding_activity"] = (
                    replay and raw.parser_version != PARSER_VERSION
                )
                try:
                    if retained_replay:
                        session.info["replay_owned_samples"] = owned_samples
                        session.info["replay_owned_intervals"] = owned_intervals
                        position = max(
                            (
                                i
                                for i, entry in enumerate(history)
                                if entry.get("raw_ref") == str(raw.id)
                            ),
                            default=-1,
                        )
                        session.info["replay_replacements"] = [
                            Replacement.restore(entry["replacement"])
                            for entry in history[position + 1 :]
                            if entry.get("replacement")
                        ]
                        session.info["replay_source_order"] = {
                            entry["raw_ref"]: i
                            for i, entry in enumerate(history)
                            if "raw_ref" in entry
                        }
                    raw.status = normalize(session, endpoint, source_key, payload, raw.id, timezone)
                finally:
                    session.info.pop("replay_owned_samples", None)
                    session.info.pop("replay_owned_intervals", None)
                    session.info.pop("replay_replacements", None)
                    session.info.pop("replay_source_order", None)
                    session.info.pop("rebuilding_activity", None)
                    session.info.pop("replaying_projection", None)
                    session.info.pop("replay_preceding_source", None)
                raw.parser_version = PARSER_VERSION
                if not unchanged:
                    record_application(
                        session, raw, history, timezone, fetched_at, contract, replay=replay
                    )
                if "replacement_snapshot" in session.info:
                    before = session.info.pop("replacement_snapshot")
                    session.info.pop("replacement_scope", None)
                    if before != interval_projection(session, source, replacement):
                        session.info["projection_changed"] = True
                if session.info.get("projection_changed"):
                    invalidate_insights(session, endpoint, timezone)
                if parser_transition and not replay:
                    from garmin_ai.replay import invalidate_outputs

                    invalidate_outputs(session)
        except Exception as exc:
            session.info.pop("replacement_snapshot", None)
            session.info.pop("replacement_scope", None)
            raw.status = "error"
            upsert(
                session,
                AppState,
                {
                    "key": metadata_key,
                    "value": {
                        **previous_metadata,
                        "timezone": timezone,
                        "failed_parser_version": PARSER_VERSION,
                    },
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
                    "value": previous_state
                    if retained_replay
                    else {**previous_state, "status": "error", "latest_attempt": attempt},
                },
                ["key"],
            )
            session.flush()
            return {"status": "error", "error_type": type(exc).__name__, "source_ref": str(raw.id)}
    successful_metadata = {
        **previous_metadata,
        "timezone": previous_metadata.get("timezone", timezone) if unchanged else timezone,
        "applied_at": fetched_at.isoformat(),
    }
    successful_metadata.pop("failed_parser_version", None)
    if unchanged and "timezone" not in previous_metadata:
        successful_metadata.pop("timezone", None)
    upsert(session, AppState, dict(key=metadata_key, value=successful_metadata), ["key"])
    value = {
        "hash": digest,
        "source_ref": str(raw.id),
        "fetched_at": datetime.now(UTC).isoformat(),
        "requested_at": fetched_at.isoformat(),
        "status": raw.status,
        "replacement": contract,
        "completeness": "adapter_attested" if replacement else "unverified",
    }
    if retained_replay:
        value = previous_state
    elif preserve_attempt:
        value.update(latest_attempt=latest_attempt, status=previous_state.get("status", raw.status))
    elif (
        raw.status == "empty"
        and replacement is None
        and previous_state.get("source_ref") != str(raw.id)
        and previous_state.get("source_ref")
    ):
        # Empty is a fetch outcome, not a replacement of retained projections.
        value = {
            **previous_state,
            "status": "empty",
            "latest_attempt": {
                **value,
                "parser_version": PARSER_VERSION,
            },
        }
    upsert(session, AppState, dict(key=state_key, value=value), ["key"])

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
            session.info["projection_changed"] = True
            # Keep the source owner while its value is rejected, so a later parser
            # can reconsider this retained revision. Newer values replace the owner.
    row.sources = sources
    session.flush()
