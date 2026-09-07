import io
import zipfile
from datetime import UTC, datetime

import fitdecode
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.models import Activity, ActivityPart, AppState, SourcePayload
from garmin_ai.normalize import PARSER_VERSION, upsert

MAX_FIT_BYTES = 100 * 1024 * 1024


def extract_fit(raw: bytes) -> list[bytes]:
    if len(raw) > MAX_FIT_BYTES:
        raise ValueError("Activity export exceeds size limit")
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        return [raw]
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = [
            e for e in archive.infolist() if e.filename.lower().endswith(".fit") and not e.is_dir()
        ]
        if not entries or sum(e.file_size for e in entries) > MAX_FIT_BYTES:
            raise ValueError("No FIT files or uncompressed archive too large")
        # Read into memory by member; never extract untrusted paths to the filesystem.
        return [archive.read(e) for e in entries]


def parse_fit(data: bytes):
    rows = []
    with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE) as reader:
        for frame in reader:
            if isinstance(frame, fitdecode.FitDataMessage) and frame.name in {
                "record",
                "lap",
                "session",
                "event",
            }:
                values = {}
                for field in frame.fields:
                    value = field.value
                    if isinstance(value, datetime):
                        value = value.isoformat()
                    elif isinstance(value, bytes):
                        value = value.hex()
                    values[field.name] = value
                rows.append((frame.name, values))
    return rows


def store_fit(session, archive, activity_id: str, raw: bytes, fetched_at=None):
    fetched_at = fetched_at or datetime.now(UTC)
    if fetched_at.tzinfo is None:
        raise ValueError("Aware fetch timestamp required")
    state_key = f"fit-version:{activity_id}"
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(state_key, 0))))
    archive_key = archive.put_bytes(raw, "zip" if zipfile.is_zipfile(io.BytesIO(raw)) else "fit")
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise LookupError("Import activity summary before FIT")
    digest = archive_key.split("/")[-1].split(".")[0]
    session.execute(
        insert(SourcePayload)
        .values(
            source="garmin_connect",
            endpoint="activity_fit",
            source_key=activity_id,
            payload_hash=digest,
            payload=None,
            archive_key=archive_key,
            fetched_at=fetched_at,
        )
        .on_conflict_do_nothing(index_elements=["source", "endpoint", "source_key", "payload_hash"])
    )
    source = session.scalar(
        select(SourcePayload)
        .where(
            SourcePayload.source == "garmin_connect",
            SourcePayload.endpoint == "activity_fit",
            SourcePayload.source_key == activity_id,
            SourcePayload.payload_hash == digest,
        )
        .with_for_update()
    )
    state = session.get(AppState, state_key, populate_existing=True)
    if state and fetched_at < datetime.fromisoformat(state.value["requested_at"]):
        if source.status == "pending":
            source.status = "stale"
        return {"status": "stale", "rows": 0}
    upsert(
        session,
        AppState,
        dict(key=state_key, value={"requested_at": fetched_at.isoformat()}),
        ["key"],
    )
    activity.fit_key = archive_key
    try:
        with session.begin_nested():
            parsed = []
            for data in extract_fit(raw):
                archive.put_bytes(data, "fit")
                parsed.extend(parse_fit(data))
            session.execute(
                delete(ActivityPart).where(
                    ActivityPart.activity_id == activity_id, ActivityPart.kind.startswith("fit_")
                )
            )
            for i, (kind, payload) in enumerate(parsed):
                upsert(
                    session,
                    ActivityPart,
                    dict(activity_id=activity_id, kind=f"fit_{kind}", sequence=i, payload=payload),
                    ["activity_id", "kind", "sequence"],
                )
            source.status = "normalized"
            source.parser_version = PARSER_VERSION
            activity.details = {
                **activity.details,
                "fit_status": "normalized",
                "parsed_fit_key": archive_key,
            }
        return {"status": "normalized", "rows": len(parsed)}
    except Exception as exc:
        source.status = "error"
        activity.details = {
            **activity.details,
            "fit_status": "error",
            "fit_error_type": type(exc).__name__,
        }
        return {"status": "error", "error_type": type(exc).__name__}
