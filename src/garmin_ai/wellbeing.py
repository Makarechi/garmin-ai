"""Subjective reports remain independent evidence, never corrected by vendor scores."""

import json
from datetime import UTC

from sqlalchemy import select

from garmin_ai.events import serialize_event
from garmin_ai.models import Event

SCALES = {
    "energy": {"min": 0, "max": 10, "higher_means": "more_reported_energy"},
    "restedness": {"min": 0, "max": 10, "higher_means": "more_reported_rest"},
    "pain": {"min": 0, "max": 10, "higher_means": "more_reported_pain"},
    "functional_impact": {"min": 0, "max": 10, "higher_means": "more_interference_with_daily_life"},
}


def label(payload):
    names = {
        "energy": "энергия",
        "restedness": "отдохнувший",
        "pain": "боль",
        "functional_impact": "помехи обычным делам",
    }
    details = [
        f"{name}: {payload[key]}/10" for key, name in names.items() if payload.get(key) is not None
    ]
    if payload.get("notes"):
        details.append(payload["notes"])
    return "Самочувствие: " + "; ".join(details)


def report_evidence(row):
    value = serialize_event(row)
    value.pop("original_text", None)
    value.pop("idempotency_key", None)
    value["omitted_fields"] = ["original_text", "idempotency_key"]
    payload = dict(value["payload"])
    notes = payload.get("notes")
    value["notes_truncated"] = bool(notes and len(notes) > 2000)
    if value["notes_truncated"]:
        payload["notes"] = notes[:2000]
    value["payload"] = payload
    return value


def observations(session, start, end):
    from garmin_ai.queries import time_range

    time_range(start, end, 31)
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    rows = session.scalars(
        select(Event)
        .where(
            Event.kind == "wellbeing_observation",
            Event.deleted.is_(False),
            Event.status == "confirmed",
            Event.source != "inferred",
            Event.start >= start,
            Event.start < end,
        )
        .order_by(Event.start, Event.id)
        .limit(201)
    ).all()
    if len(rows) > 200:
        raise ValueError("More than 200 reports; narrow the range")
    result = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "scales": SCALES,
        "rows": [report_evidence(row) for row in rows],
        "evidence_type": "subjective_diary",
        "missingness": "unreported_is_unknown",
        "limitations": [
            "Reported wellbeing and Garmin scores are distinct outcomes; retain disagreement",
            "Numeric ratings are recorded only when explicitly reported; notes imply no numeric rating",
            "No reports do not establish absence of symptoms or good wellbeing",
        ],
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 20000:
        raise ValueError("Report content exceeds response budget; narrow the range")
    return result
