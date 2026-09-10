"""Subjective reports remain independent evidence, never corrected by vendor scores."""

import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, tuple_

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


def observations(session, start, end, cursor=None):
    from garmin_ai.queries import time_range

    time_range(start, end, 31)
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    query = (
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
    )
    if cursor is not None:
        try:
            at, identity = json.loads(cursor)
            at, identity = datetime.fromisoformat(at), UUID(identity)
            if at.tzinfo is None or not start <= at < end:
                raise ValueError()
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("Invalid report cursor") from exc
        query = query.where(tuple_(Event.start, Event.id) > tuple_(at, identity))
    rows = session.scalars(query.limit(201)).all()
    result = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "scales": SCALES,
        "rows": [],
        "next_cursor": None,
        "truncated": False,
        "evidence_type": "subjective_diary",
        "missingness": "unreported_is_unknown",
        "limitations": [
            "Reported wellbeing and Garmin scores are distinct outcomes; retain disagreement",
            "Numeric ratings are recorded only when explicitly reported; notes imply no numeric rating",
            "No reports do not establish absence of symptoms or good wellbeing",
        ],
    }
    for row in rows[:200]:
        result["rows"].append(report_evidence(row))
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 19000:
            result["rows"].pop()
            break
    if len(result["rows"]) < len(rows):
        last = rows[len(result["rows"]) - 1]
        result["next_cursor"] = json.dumps([last.start.isoformat(), str(last.id)])
        result["truncated"] = True
    return result
