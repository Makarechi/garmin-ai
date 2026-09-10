"""Activity-scoped FIT device/settings evidence with an explicit public field allowlist."""

import json
import math
from datetime import UTC

from sqlalchemy import select

from garmin_ai.models import Activity, ActivityPart, SourcePayload
from garmin_ai.normalize import PARSER_VERSION
from garmin_ai.queries import time_range

FIELDS = {
    "fit_device_info": {
        "device_index",
        "device_type",
        "manufacturer",
        "software_version",
        "hardware_version",
        "sensor_position",
        "source_type",
    },
    "fit_hr_zone": {"high_bpm", "message_index"},
    "fit_zones_target": {
        "max_heart_rate",
        "threshold_heart_rate",
        "functional_threshold_power",
        "hr_calc_type",
        "pwr_calc_type",
    },
    "fit_ohr_settings": {"enabled"},
    "fit_hrm_profile": {"enabled", "log_hrv", "message_index"},
}


def history(session, start, end, limit=100):
    time_range(start, end, 366)
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if not 1 <= limit <= 200:
        raise ValueError("Use a positive range up to 366 days and limit 1..200")
    activities = session.scalars(
        select(Activity)
        .where(Activity.start >= start, Activity.start < end)
        .order_by(Activity.start, Activity.id)
        .limit(limit + 1)
    ).all()
    rows = []
    byte_truncated = False
    for activity in activities[:limit]:
        parts = session.scalars(
            select(ActivityPart)
            .where(ActivityPart.activity_id == activity.id, ActivityPart.kind.in_(FIELDS))
            .order_by(ActivityPart.sequence, ActivityPart.kind)
            .limit(201)
        ).all()
        evidence = []
        for part in parts[:200]:
            fields = {}
            for key in sorted(FIELDS[part.kind]):
                value = part.payload.get(key)
                if value is None or not isinstance(value, (str, int, float, bool)):
                    continue
                if isinstance(value, str) and len(value) > 80:
                    continue
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                fields[key] = value
            evidence.append({"kind": part.kind, "sequence": part.sequence, "fields": fields})
        source = session.scalar(
            select(SourcePayload)
            .where(
                SourcePayload.source == "garmin_connect",
                SourcePayload.endpoint == "activity_fit",
                SourcePayload.source_key == activity.id,
                SourcePayload.archive_key == activity.details.get("parsed_fit_key"),
            )
            .order_by(SourcePayload.id)
            .limit(1)
        )
        current = (
            bool(activity.fit_key)
            and activity.details.get("parsed_fit_key") == activity.fit_key
            and source is not None
            and source.status == "normalized"
            and source.parser_version == PARSER_VERSION
        )
        rows.append(
            {
                "activity_id": activity.id,
                "start": activity.start.isoformat(),
                "sport": activity.kind,
                "evidence_status": "available"
                if evidence and current
                else "stale"
                if evidence
                else "unavailable",
                "fit_archive_ref": activity.details.get("parsed_fit_key"),
                "source_ref": str(source.id) if source else None,
                "parser_version": source.parser_version if source else None,
                "records": evidence,
                "records_truncated": len(parts) > 200,
            }
        )
        # Reserve space for envelope/limitations and stop reading later activities.
        if len(json.dumps(rows, ensure_ascii=False).encode()) > 35000:
            byte_truncated = True
            if len(rows) > 1:
                rows.pop()
            else:
                while (
                    rows[0]["records"]
                    and len(json.dumps(rows, ensure_ascii=False).encode()) > 35000
                ):
                    rows[0]["records"].pop()
                    rows[0]["records_truncated"] = True
            break
    result = {
        "rows": rows,
        "truncated": byte_truncated or len(activities) > limit,
        "limitations": [
            "Activity-scoped observations, not a complete device inventory or continuous settings history",
            "Device index is local to a FIT file; matching indices across activities do not establish identity",
            "Recorded device presence does not prove which sensor supplied heart-rate samples",
            "Zones are preserved as recorded; no physiological zone inference or historical recalculation",
            "Missing evidence means unavailable; stale evidence is from an older FIT projection",
        ],
    }
    while len(json.dumps(result, ensure_ascii=False).encode()) > 40000:
        result["truncated"] = True
        if len(rows) > 1:
            rows.pop()
        elif rows and rows[0]["records"]:
            rows[0]["records"].pop()
            rows[0]["records_truncated"] = True
        else:
            rows.clear()
            break
    return result
