"""Activity-scoped FIT device/settings evidence with an explicit public field allowlist."""

import json
import math
from datetime import timedelta

from sqlalchemy import select

from garmin_ai.models import Activity, ActivityPart

FIELDS = {
    "fit_device_info": {
        "device_index",
        "device_type",
        "manufacturer",
        "product",
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
    if end <= start or end - start > timedelta(days=366) or not 1 <= limit <= 200:
        raise ValueError("Use a positive range up to 366 days and limit 1..200")
    activities = session.scalars(
        select(Activity)
        .where(Activity.start >= start, Activity.start < end)
        .order_by(Activity.start, Activity.id)
        .limit(limit + 1)
    ).all()
    rows = []
    for activity in activities[:limit]:
        parts = session.scalars(
            select(ActivityPart)
            .where(ActivityPart.activity_id == activity.id, ActivityPart.kind.in_(FIELDS))
            .order_by(ActivityPart.kind, ActivityPart.sequence)
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
        current = (
            bool(activity.fit_key) and activity.details.get("parsed_fit_key") == activity.fit_key
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
                "records": evidence,
                "records_truncated": len(parts) > 200,
            }
        )
    result = {
        "rows": rows,
        "truncated": len(activities) > limit,
        "limitations": [
            "Activity-scoped observations, not a complete device inventory or continuous settings history",
            "Device index is local to a FIT file; matching indices across activities do not establish identity",
            "Recorded device presence does not prove which sensor supplied heart-rate samples",
            "Zones are preserved as recorded; no physiological zone inference or historical recalculation",
            "Missing evidence means unavailable; stale evidence is from an older FIT projection",
        ],
    }
    if len(json.dumps(result, ensure_ascii=False).encode()) > 40000:
        raise ValueError("Device evidence exceeds budget; narrow the range or lower limit")
    return result
