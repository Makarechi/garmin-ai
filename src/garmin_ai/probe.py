"""Account coverage investigation. Reports structure, archives values locally."""

from datetime import UTC, date, datetime, timedelta

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


def probe(
    reader: GarminReader, archive: LocalArchive, start: date, end: date, checkpoint=None
) -> dict:
    if not 0 <= (end - start).days < 31:
        raise ValueError("Probe range must be 1–31 days")
    report = {"start": str(start), "end": str(end), "requests": [], "complete": False}

    def save():
        if checkpoint:
            checkpoint(report)

    def capture(endpoint, key, fetch, binary=False):
        row = {"endpoint": endpoint, "key": key, "fetched_at": datetime.now(UTC).isoformat()}
        try:
            payload = fetch()
        except Exception as exc:
            row.update(status="error", error_type=type(exc).__name__)
            report["requests"].append(row)
            save()
            if isinstance(exc, (AuthenticationRequired, CircuitOpen)):
                raise
            return None
        # Local serialization/storage failures must stop requests, not masquerade
        # as upstream endpoint failures. Earlier checkpoint mappings remain valid.
        row.update(status="empty" if payload in (None, {}, [], b"") else "available")
        row["archive_key"] = (
            archive.put_bytes(payload, "zip") if binary else archive.put_json(payload)
        )
        row["shape"] = {"type": "bytes", "size": len(payload)} if binary else shape(payload)
        report["requests"].append(row)
        save()
        return payload

    save()
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        for endpoint in ENDPOINTS:
            if endpoint.scope == "day":
                capture(endpoint.name, str(day), lambda e=endpoint, d=day: reader.fetch(e, d))
    for endpoint in ENDPOINTS:
        if endpoint.scope == "global":
            capture(endpoint.name, "global", lambda e=endpoint: reader.fetch(e))
    activities = capture("activities", "recent", lambda: reader.call("get_activities", 0, 100))
    if activities is not None:
        report["activity_list_archive"] = report["requests"][-1]["archive_key"]
        if not isinstance(activities, list):
            raise ValueError("Unexpected activity list shape")
        for activity in activities[:5]:
            activity_id = str(activity["activityId"])
            for endpoint in ENDPOINTS:
                if endpoint.scope == "activity":
                    capture(
                        endpoint.name,
                        activity_id,
                        lambda e=endpoint, a=activity_id: reader.fetch(e, activity_id=a),
                    )
            capture(
                "activity_fit",
                activity_id,
                lambda a=activity_id: reader.call(
                    "download_activity", a, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
                ),
                binary=True,
            )
    report["complete"] = True
    save()
    return report
