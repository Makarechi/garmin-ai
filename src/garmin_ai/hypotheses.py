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
    validation_start: date = Field(description="Start 3 to 31 local dates after registration")
    validation_end: date = Field(description="Validation spans 28 to 31 inclusive dates")
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
        if (self.validation_end - self.validation_start).days < 27:
            raise ValueError("Directional validation requires at least 28 inclusive dates")
        if not self.discovery_end < self.validation_start <= self.validation_end < self.expires:
            raise ValueError("Discovery, prospective validation and expiry must be ordered")
        if not 2 <= (self.expires - self.validation_end).days <= 31 or not self.question.strip():
            raise ValueError("Expiry must be 2 to 31 days after validation end")
        return self


def fetch(session, identity):
    row = session.get(AppState, PREFIX + str(identity), populate_existing=True)
    if row is None:
        raise LookupError("Hypothesis not found")
    return row


def today(session, spec, now):
    from garmin_ai.config import Settings

    timezone = session.info.get("timezone") or Settings().timezone
    if spec.timezone != timezone:
        raise ValueError("Hypothesis timezone must match the configured data timezone")
    return now.astimezone(ZoneInfo(timezone)).date()


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
    current = today(session, spec, now)
    if not spec.discovery_end < current < spec.validation_start:
        raise ValueError("Register after discovery and before prospective validation starts")
    if not 3 <= (spec.validation_start - current).days <= 31:
        raise ValueError(
            "Prospective exposure needs a buffer: start 3 to 31 days after registration"
        )
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
        "method_version": discovery["spec"]["method_version"],
        "status": "registered",
        "discovery": discovery,
        "checks": [],
        "check_count": 0,
        "current_check_index": None,
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
    current = today(session, spec, now)
    if value["status"] == "stopped" or current >= spec.expires:
        raise Conflict("Hypothesis stopped or expired")
    if current <= spec.validation_end:
        raise ValueError("Validation period has not finished")
    from garmin_ai.coffee_sleep import VERSION

    method = value.get("method_version") or value["discovery"].get("spec", {}).get("method_version")
    if method != VERSION:
        raise Conflict("Registered analyzer version is unavailable; register a new protocol")
    result = guarded_analysis(session, spec, spec.validation_start, spec.validation_end)
    if result.get("spec", {}).get("method_version") != method:
        raise Conflict("Validation analyzer version differs from the registered protocol")
    registered_at = datetime.fromisoformat(value["created_at"])
    if any(
        datetime.fromisoformat(item["exposure_start"]) < registered_at
        for item in result.get("rows", [])
        if item.get("exposure_start")
    ):
        raise Conflict("Validation exposure predates prospective registration")
    previous = value["checks"]
    for index, check in enumerate(previous):
        if check["evidence"]["evidence_hash"] == result["evidence_hash"]:
            row.value = {**value, "current_check_index": index, "last_checked_at": now.isoformat()}
            session.flush()
            return row.value
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
        discovery = value["discovery"]
        discovery_interval = (discovery.get("comparison") or {}).get("ci95")
        supported = (
            discovery.get("status") != "insufficient_evidence"
            and discovery_interval
            and (
                (spec.direction == "lower" and discovery_interval[1] < 0)
                or (spec.direction == "higher" and discovery_interval[0] > 0)
            )
        )
        conclusion = (
            "direction_repeated_observationally"
            if supported
            else "validation_direction_supported_observationally"
        )
    elif interval and (interval[1] < 0 or interval[0] > 0):
        conclusion = "opposite_direction"
    check = {"checked_at": now.isoformat(), "conclusion": conclusion, "evidence": result}
    row.value = {
        **value,
        "status": "checked",
        "checks": [*previous, check],
        "check_count": len(previous) + 1,
        "current_check_index": len(previous),
        "last_checked_at": now.isoformat(),
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
