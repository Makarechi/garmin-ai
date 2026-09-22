"""Explicit owner preferences; never inferred from device metrics or model output."""

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import text

from garmin_ai.accounts import AccountMismatch, owner
from garmin_ai.db import transaction
from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.models import AppState
from garmin_ai.normalize import upsert

KEY = "preferences:personal-goals"
LABELS = {"wellbeing": "самочувствие", "sleep": "сон", "running": "бег", "migraine": "мигрень"}
Goal = Literal["wellbeing", "sleep", "running", "migraine"]


class GoalSelection(StrictModel):
    revision: int = Field(ge=0, strict=True)
    goals: list[Goal] = Field(max_length=4)

    @model_validator(mode="after")
    def distinct(self):
        if len(self.goals) != len(set(self.goals)):
            raise ValueError("Goals must be distinct")
        return self


class CommandOrder(StrictModel):
    """Channel-neutral ordering evidence supplied by trusted ingress."""

    source_epoch: int
    received_at: datetime
    tie_breaker: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def aware(self):
        if self.received_at.utcoffset() is None:
            raise ValueError("Command receipt time must be aware")
        return self

    @property
    def key(self):
        return (self.source_epoch, self.received_at, self.tie_breaker)


def preferences(session):
    person = owner(session)
    row = session.get(AppState, KEY, populate_existing=True)
    if row is None:
        return {"configured": False, "revision": 0, "goals": [], "updated_at": None}
    bound_owner = row.value.get("owner_id")
    if bound_owner is not None and bound_owner != str(person.id):
        raise AccountMismatch("Tracker preferences belong to another owner")
    if bound_owner is None:
        row.value = {**row.value, "owner_id": str(person.id)}
        session.flush()
    return {key: row.value[key] for key in ("configured", "revision", "goals", "updated_at")}


def select_goals(session, selection, now=None, *, command_order: CommandOrder | None = None):
    selection = GoalSelection.model_validate(selection.model_dump())
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("Goal preference clock must be aware")
    lock_writes(session)
    session.execute(text("SELECT pg_advisory_xact_lock(72104626)"))
    person = owner(session)
    current = preferences(session)
    row = session.get(AppState, KEY)
    previous_order = row.value.get("command_order") if row else None
    api_changed_at = row.value.get("api_changed_at") if row else None
    if command_order is not None and api_changed_at is not None:
        if command_order.source_epoch < int(api_changed_at) or (
            command_order.source_epoch == int(api_changed_at)
            and command_order.received_at.timestamp() <= api_changed_at
        ):
            return current
    if command_order is not None and previous_order:
        previous = CommandOrder.model_validate(previous_order)
        if command_order.key <= previous.key:
            return current
    if command_order is not None and not previous_order and row:
        legacy_order = row.value.get("telegram_order")
        if legacy_order and command_order.source_epoch <= legacy_order[0]:
            # Preserve the old fence without treating future opaque IDs as counters.
            if command_order.source_epoch < legacy_order[0] or command_order.tie_breaker == str(
                legacy_order[1]
            ):
                return current
    if current["revision"] != selection.revision:
        raise Conflict("Goal preferences changed; reload before updating")
    goals = sorted(selection.goals)
    if current["configured"] and current["goals"] == goals:
        if command_order is not None:
            row.value = {
                **row.value,
                "command_order": command_order.model_dump(mode="json"),
            }
            session.flush()
        else:
            row.value = {**row.value, "api_changed_at": now.timestamp()}
            session.flush()
        return current
    history = row.value.get("history", []) if row else []
    value = {
        "owner_id": str(person.id),
        "configured": True,
        "revision": current["revision"] + 1,
        "goals": goals,
        "updated_at": now.isoformat(),
        "history": [*history, current][-20:],
        "api_changed_at": now.timestamp() if command_order is None else api_changed_at,
        "command_order": command_order.model_dump(mode="json")
        if command_order is not None
        else previous_order,
    }
    upsert(session, AppState, {"key": KEY, "value": value}, ["key"])
    session.flush()
    return preferences(session)


def revision_matches(session, revision, *, lock=False):
    if lock:
        lock_writes(session)
    return preferences(session)["revision"] == revision


def telegram_goals(session, command, now, *, sent_at=None, received_at=None, update_id=0):
    lock_writes(session)
    parts = command.casefold().replace(",", " ").split()[1:]
    if parts:
        names = {label: key for key, label in LABELS.items()}
        try:
            selected = [] if parts == ["нет"] else [names[part] for part in parts]
            lock_writes(session)
            select_goals(
                session,
                GoalSelection(revision=preferences(session)["revision"], goals=selected),
                now,
                command_order=CommandOrder(
                    source_epoch=int((sent_at or now).timestamp()),
                    received_at=received_at or now,
                    tie_breaker=str(update_id),
                ),
            )
        except (KeyError, ValueError):
            return "Не удалось выбрать цели. Используйте: /goals сон самочувствие бег мигрень. Можно выбрать часть списка; /goals нет — убрать все цели."
    value = preferences(session)
    session.info["goals_revision"] = value["revision"]
    if not value["configured"]:
        description = "Цели пока не выбраны."
    elif not value["goals"]:
        description = "Личные цели отключены."
    else:
        description = "Ваши цели: " + ", ".join(LABELS[key] for key in value["goals"]) + "."
    if value["configured"] and "running" not in value["goals"]:
        description += " Спортивная цель выключена."
    return (
        description
        + "\nВыберите нужные: /goals сон самочувствие бег мигрень. Убрать все: /goals нет."
    )


@contextmanager
def delivery_guard(engine, revision):
    """Serialize the first network send with committed goal edits."""
    with transaction(engine) as session:
        session.execute(text("SELECT pg_advisory_xact_lock_shared(72104626)"))
        yield revision_matches(session, revision)
