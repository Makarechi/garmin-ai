from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.archive import LocalArchive
from garmin_ai.models import AppState, SourcePayload
from garmin_ai.normalize import PARSER_VERSION, normalize, upsert


def ingest(
    session,
    archive: LocalArchive,
    endpoint: str,
    source_key: str,
    payload,
    timezone: str,
    source="garmin_connect",
):
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
    state = session.get(AppState, state_key)
    # A -> B -> A is a legitimate upstream correction, not an identical replay.
    unchanged = (
        state
        and state.value.get("hash") == digest
        and raw.parser_version == PARSER_VERSION
        and raw.status not in {"pending", "error"}
    )
    if not unchanged:
        try:
            with session.begin_nested():
                raw.status = normalize(session, endpoint, source_key, payload, raw.id, timezone)
                raw.parser_version = PARSER_VERSION
        except Exception as exc:
            raw.status = "error"
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
                "status": raw.status,
            },
        ),
        ["key"],
    )
    return {"status": "unchanged" if unchanged else raw.status, "source_ref": str(raw.id)}
