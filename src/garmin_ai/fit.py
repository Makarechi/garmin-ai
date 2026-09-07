import io
import zipfile
from datetime import datetime

import fitdecode

from garmin_ai.models import Activity, ActivityPart
from garmin_ai.normalize import upsert

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


def store_fit(session, archive, activity_id: str, raw: bytes):
    archive_key = archive.put_bytes(raw, "zip" if zipfile.is_zipfile(io.BytesIO(raw)) else "fit")
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise LookupError("Import activity summary before FIT")
    activity.fit_key = archive_key
    parsed = []
    for data in extract_fit(raw):
        archive.put_bytes(data, "fit")
        parsed.extend(parse_fit(data))
    for i, (kind, payload) in enumerate(parsed):
        upsert(
            session,
            ActivityPart,
            dict(activity_id=activity_id, kind=f"fit_{kind}", sequence=i, payload=payload),
            ["activity_id", "kind", "sequence"],
        )
    return len(parsed)
