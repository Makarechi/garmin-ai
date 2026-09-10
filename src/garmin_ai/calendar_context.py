"""Opt-in calendar plans, isolated from confirmed diary facts and model prompts."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select

from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.models import AppState
from garmin_ai.queries import time_range

PREFIX = "calendar-context:"
Category = Literal["work", "personal", "travel", "exercise", "other"]


class CalendarItem(StrictModel):
    source_id: UUID
    id: UUID
    revision: int = Field(ge=1)
    status: Literal["busy", "cancelled"]
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    timezone: str | None = None
    category: Category | None = None

    @model_validator(mode="after")
    def plan_interval(self):
        if self.status == "busy":
            if None in (self.start, self.end, self.timezone, self.category):
                raise ValueError("Busy plans require interval, timezone and coarse category")
            time_range(self.start, self.end, 31)
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Unknown calendar timezone") from None
        elif any(
            value is not None for value in (self.start, self.end, self.timezone, self.category)
        ):
            raise ValueError("Cancellation contains identity and revision only")
        return self


class CalendarBatch(StrictModel):
    items: list[CalendarItem] = Field(min_length=1, max_length=100)


def allowed_sources(settings, now):
    result = {}
    for source in settings.calendar_sources:
        if source.id in result:
            raise ValueError("Duplicate configured calendar source")
        if source.granted_at <= now:
            result[source.id] = source.categories
    return result


def import_batch(session, settings, batch, now=None):
    with session.begin_nested():
        return _import_batch(session, settings, batch, now)


def _import_batch(session, settings, batch, now=None):
    now = now or datetime.now(UTC)
    batch = CalendarBatch.model_validate(batch.model_dump())
    allowed = allowed_sources(settings, now)
    lock_writes(session)
    count = session.scalar(
        select(func.count()).select_from(AppState).where(AppState.key.startswith(PREFIX))
    )
    outcomes = []
    for item in batch.items:
        if item.source_id not in allowed:
            raise ValueError("Calendar source is not enabled")
        if item.status == "busy" and item.category not in allowed[item.source_id]:
            raise ValueError("Calendar category is not enabled")
        key = f"{PREFIX}{item.source_id}:{item.id}"
        value = item.model_dump(mode="json")
        if item.start:
            value["start"] = item.start.astimezone(UTC).isoformat()
            value["end"] = item.end.astimezone(UTC).isoformat()
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        row = session.get(AppState, key, populate_existing=True)
        if row:
            previous = row.value
            if item.revision < previous["item"]["revision"]:
                prior = next(
                    (
                        record
                        for record in previous["previous_revisions"]
                        if record["revision"] == item.revision
                    ),
                    None,
                )
                if prior is not None and prior["hash"] != digest:
                    raise Conflict("Calendar revision was reused with different content")
                outcomes.append({"id": str(item.id), "status": "stale"})
                continue
            if item.revision == previous["item"]["revision"]:
                if digest != previous["hash"]:
                    raise Conflict("Calendar revision was reused with different content")
                outcomes.append({"id": str(item.id), "status": "unchanged"})
                continue
            history = [
                *previous["previous_revisions"],
                {"revision": previous["item"]["revision"], "hash": previous["hash"]},
            ][-20:]
        else:
            if count >= 2000:
                raise ValueError("Calendar storage limit reached")
            count += 1
            history = []
        stored = {
            "item": value,
            "hash": digest,
            "received_at": now.isoformat(),
            "previous_revisions": history,
        }
        if row:
            row.value = stored
        else:
            session.add(AppState(key=key, value=stored))
        session.flush()
        outcomes.append({"id": str(item.id), "status": item.status})
    return {"outcomes": outcomes}


def plans(session, settings, start, end, now=None):
    time_range(start, end, 31)
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    allowed = allowed_sources(settings, now or datetime.now(UTC))
    rows = []
    if allowed:
        for state in session.scalars(
            select(AppState)
            .where(AppState.key.startswith(PREFIX))
            .order_by(AppState.key)
            .limit(2001)
        ):
            item = state.value["item"]
            source = UUID(item["source_id"])
            if (
                source not in allowed
                or item["status"] != "busy"
                or item["category"] not in allowed[source]
            ):
                continue
            if (
                datetime.fromisoformat(item["start"]) < end
                and datetime.fromisoformat(item["end"]) > start
            ):
                rows.append(
                    {
                        **item,
                        "evidence_hash": state.value["hash"],
                        "received_at": state.value["received_at"],
                    }
                )
    return {
        "availability": "enabled" if allowed else "disabled",
        "rows": rows,
        "evidence_type": "calendar_plan",
        "limitations": [
            "A busy interval is a plan, not evidence of attendance, physical activity or stress",
            "Missing plans do not establish free time",
            "Only explicitly enabled calendars and categories are included",
            "No calendar text is sent to a language model",
        ],
    }
