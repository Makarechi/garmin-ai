from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.archive import LocalArchive
from garmin_ai.models import AppState, Measurement, SourcePayload
from garmin_ai.normalize import PARSER_VERSION, normalize, upsert
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
    if (
        state
        and state.value.get("requested_at")
        and fetched_at < datetime.fromisoformat(state.value["requested_at"])
    ):
        if raw.status == "pending":
            raw.status = "stale"
        return {"status": "stale", "source_ref": str(raw.id)}
    metadata_key = f"ingest-meta:{raw.id}"
    if session.get(AppState, metadata_key) is None:
        upsert(session, AppState, dict(key=metadata_key, value={"timezone": timezone}), ["key"])
    # A -> B -> A is a legitimate upstream correction, not an identical replay.
    unchanged = (
        state
        and state.value.get("hash") == digest
        and raw.parser_version == PARSER_VERSION
        and raw.status not in {"pending", "error"}
        and state.value.get("replacement") == contract
    )
    shared_targets = endpoint in {"activity", "activities", "daily", "heart_rate", "body_battery"}
    if not unchanged or shared_targets:
        try:
            with session.begin_nested():
                if (rebuild_projection or raw.parser_version != PARSER_VERSION) and not unchanged:
                    # Rebuild only this immutable raw revision; older partial
                    # revisions may still own observations absent from this payload.
                    session.execute(delete(Measurement).where(Measurement.source_ref == raw.id))
                if replacement and not unchanged:
                    replace_interval(session, source, endpoint, source_key, replacement)
                session.info["fetch_time"] = fetched_at
                session.info["skip_samples"] = unchanged
                raw.status = normalize(session, endpoint, source_key, payload, raw.id, timezone)
                raw.parser_version = PARSER_VERSION
                if not unchanged:
                    invalidate_insights(session)
        except Exception as exc:
            raw.status = "error"
            upsert(
                session,
                AppState,
                dict(
                    key=state_key,
                    value={
                        "hash": digest,
                        "source_ref": str(raw.id),
                        "requested_at": fetched_at.isoformat(),
                        "status": "error",
                        "replacement": contract,
                        "completeness": "adapter_attested" if replacement else "unverified",
                    },
                ),
                ["key"],
            )
            session.flush()
            return {"status": "error", "error_type": type(exc).__name__, "source_ref": str(raw.id)}
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
