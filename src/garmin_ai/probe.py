"""Account coverage investigation. Reports structure, archives values locally."""

from datetime import date, timedelta

from garminconnect import Garmin

from garmin_ai.archive import LocalArchive
from garmin_ai.garmin import ENDPOINTS, AuthenticationRequired, CircuitOpen, GarminReader


def shape(value: object, depth: int = 0) -> object:
    """Describe a response without retaining its scalar values."""
    if depth >= 8:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k): shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        variants = []
        for item in value[:100]:
            item_shape = shape(item, depth + 1)
            if item_shape not in variants:
                variants.append(item_shape)
        return {"type": "array", "count": len(value), "items": variants}
    return type(value).__name__


def probe(reader: GarminReader, archive: LocalArchive, start: date, end: date) -> dict:
    if not 0 <= (end - start).days < 31:
        raise ValueError("Probe range must be 1–31 days")
    report = {"start": str(start), "end": str(end), "requests": []}

    def capture(endpoint, key, fetch):
        row = {"endpoint": endpoint, "key": key}
        try:
            payload = fetch()
            row.update(status="empty" if payload in (None, {}, []) else "available")
            row["archive_key"] = archive.put_json(payload)
            row["shape"] = shape(payload)
        except (AuthenticationRequired, CircuitOpen):
            raise
        except Exception as exc:
            # Exception messages can contain identifiers or response bodies.
            row.update(status="error", error_type=type(exc).__name__)
        report["requests"].append(row)

    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        for endpoint in ENDPOINTS:
            if endpoint.scope == "day":
                capture(endpoint.name, str(day), lambda e=endpoint, d=day: reader.fetch(e, d))
    for endpoint in ENDPOINTS:
        if endpoint.scope == "global":
            capture(endpoint.name, "global", lambda e=endpoint: reader.fetch(e))
    activities = reader.call("get_activities", 0, 100)
    if not isinstance(activities, list):
        raise ValueError("Unexpected activity list shape")
    report["activity_list_archive"] = archive.put_json(activities)
    # Deliberately bounded reconnaissance, not the full historical importer.
    for activity in activities[:5]:
        activity_id = str(activity["activityId"])
        for endpoint in ENDPOINTS:
            if endpoint.scope == "activity":
                capture(
                    endpoint.name,
                    activity_id,
                    lambda e=endpoint, a=activity_id: reader.fetch(e, activity_id=a),
                )
        try:
            raw = reader.call(
                "download_activity", activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
            )
            key = archive.put_bytes(raw, "zip")
            report["requests"].append(
                {
                    "endpoint": "activity_fit",
                    "key": activity_id,
                    "status": "available",
                    "archive_key": key,
                }
            )
        except (AuthenticationRequired, CircuitOpen):
            raise
        except Exception as exc:
            report["requests"].append(
                {
                    "endpoint": "activity_fit",
                    "key": activity_id,
                    "status": "error",
                    "error_type": type(exc).__name__,
                }
            )
    return report
