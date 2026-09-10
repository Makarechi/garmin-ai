"""Explicit prospective observational protocols; no interventions or recommendations."""

from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from sqlalchemy import func, select

from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.models import AppState
from garmin_ai.queries import date_range

PREFIX = "hypothesis:"


class HypothesisSpec(StrictModel):
    id: UUID
    question: str = Field(min_length=1, max_length=1000)
    discovery_start: date
    discovery_end: date
    validation_start: date
    validation_end: date
    expires: date
    timezone: str
    outcome: Literal["sleep_score", "sleep_seconds"] = "sleep_score"
    late_hours: float = Field(default=6, gt=0, le=24)
    direction: Literal["lower", "higher"]

    @model_validator(mode="after")
    def bounded(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown timezone") from None
        date_range(self.discovery_start, self.discovery_end, 30)
        date_range(self.validation_start, self.validation_end, 30)
        if not self.discovery_end < self.validation_start <= self.validation_end < self.expires:
            raise ValueError("Discovery, prospective validation and expiry must be ordered")
        if (self.expires - self.validation_end).days > 31 or not self.question.strip():
            raise ValueError("Expiry must be within 31 days of validation end")
        return self


def fetch(session, identity):
    row = session.get(AppState, PREFIX + str(identity), populate_existing=True)
    if row is None:
        raise LookupError("Hypothesis not found")
    return row


def today(spec, now):
    return now.astimezone(ZoneInfo(spec.timezone)).date()


def guarded_analysis(session, spec, start, end):
    from garmin_ai.tools import call_tool

    return call_tool(
        session,
        "analysis_coffee_sleep",
        {"start": start, "end": end, "late_hours": spec.late_hours, "outcome": spec.outcome},
    )


def register(session, spec, now=None):
    spec = HypothesisSpec.model_validate(spec.model_dump())
    now = now or datetime.now(UTC)
    lock_writes(session)
    values = spec.model_dump(mode="json")
    existing = session.get(AppState, PREFIX + str(spec.id), populate_existing=True)
    if existing:
        if existing.value["spec"] != values:
            raise Conflict("Registered hypothesis specification is immutable")
        return existing.value
    current = today(spec, now)
    if not spec.discovery_end < current < spec.validation_start:
        raise ValueError("Register after discovery and before prospective validation starts")
    if (spec.validation_start - current).days > 31:
        raise ValueError("Validation must start within 31 days")
    if (
        session.scalar(
            select(func.count()).select_from(AppState).where(AppState.key.startswith(PREFIX))
        )
        >= 100
    ):
        raise ValueError("Hypothesis storage limit reached")
    discovery = guarded_analysis(session, spec, spec.discovery_start, spec.discovery_end)
    value = {
        "spec": values,
        "created_at": now.isoformat(),
        "status": "registered",
        "discovery": discovery,
        "checks": [],
        "check_count": 0,
        "interpretation": "Observational protocol, not a treatment experiment or verified recommendation",
    }
    session.add(AppState(key=PREFIX + str(spec.id), value=value))
    session.flush()
    return value


def recheck(session, identity, now=None):
    now = now or datetime.now(UTC)
    lock_writes(session)
    row = fetch(session, identity)
    value = row.value
    spec = HypothesisSpec.model_validate(value["spec"])
    current = today(spec, now)
    if value["status"] == "stopped" or current >= spec.expires:
        raise Conflict("Hypothesis stopped or expired")
    if current <= spec.validation_end:
        raise ValueError("Validation period has not finished")
    result = guarded_analysis(session, spec, spec.validation_start, spec.validation_end)
    previous = value["checks"]
    if previous and previous[-1]["evidence"]["evidence_hash"] == result["evidence_hash"]:
        return value
    if len(previous) >= 10:
        raise ValueError("Recheck history limit reached; retained results are not overwritten")
    comparison = result["comparison"] or {}
    interval = comparison.get("ci95")
    conclusion = "not_distinguished"
    if result["status"] == "insufficient_evidence":
        conclusion = "insufficient_evidence"
    elif interval and (
        (spec.direction == "lower" and interval[1] < 0)
        or (spec.direction == "higher" and interval[0] > 0)
    ):
        conclusion = "direction_repeated_observationally"
    elif interval and (interval[1] < 0 or interval[0] > 0):
        conclusion = "opposite_direction"
    check = {"checked_at": now.isoformat(), "conclusion": conclusion, "evidence": result}
    row.value = {
        **value,
        "status": "checked",
        "checks": [*previous, check],
        "check_count": len(previous) + 1,
    }
    session.flush()
    return row.value


def stop(session, identity, now=None):
    lock_writes(session)
    row = fetch(session, identity)
    if row.value["status"] != "stopped":
        row.value = {
            **row.value,
            "status": "stopped",
            "stopped_at": (now or datetime.now(UTC)).isoformat(),
        }
    return row.value
