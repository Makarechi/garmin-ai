"""Explicit owner preferences; never inferred from device metrics or model output."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.models import AppState, TelegramUpdate
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


def preferences(session):
    row = session.get(AppState, KEY, populate_existing=True)
    if row is None:
        return {"configured": False, "revision": 0, "goals": [], "updated_at": None}
    return {key: row.value[key] for key in ("configured", "revision", "goals", "updated_at")}


def select_goals(session, selection, now=None, *, message_order=None):
    selection = GoalSelection.model_validate(selection.model_dump())
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("Goal preference clock must be aware")
    lock_writes(session)
    current = preferences(session)
    row = session.get(AppState, KEY)
    previous_order = row.value.get("telegram_order") if row else None
    api_changed_at = row.value.get("api_changed_at") if row else None
    if message_order is not None and api_changed_at is not None:
        update = session.get(TelegramUpdate, message_order[1])
        received_at = update.received_at.timestamp() if update else now.timestamp()
        if message_order[0] < int(api_changed_at) or (
            message_order[0] == int(api_changed_at) and received_at <= api_changed_at
        ):
            return current
    if (
        message_order is not None
        and previous_order
        and tuple(message_order) <= tuple(previous_order)
    ):
        return current
    if current["revision"] != selection.revision:
        raise Conflict("Goal preferences changed; reload before updating")
    goals = sorted(selection.goals)
    if current["configured"] and current["goals"] == goals:
        if message_order is not None:
            row.value = {**row.value, "telegram_order": list(message_order)}
            session.flush()
        else:
            row.value = {**row.value, "api_changed_at": now.timestamp()}
            session.flush()
        return current
    history = row.value.get("history", []) if row else []
    value = {
        "configured": True,
        "revision": current["revision"] + 1,
        "goals": goals,
        "updated_at": now.isoformat(),
        "history": [*history, current][-20:],
        "api_changed_at": now.timestamp() if message_order is None else api_changed_at,
        "telegram_order": list(message_order) if message_order is not None else previous_order,
    }
    upsert(session, AppState, {"key": KEY, "value": value}, ["key"])
    session.flush()
    return preferences(session)


def revision_matches(session, revision, *, lock=False):
    if lock:
        lock_writes(session)
    return preferences(session)["revision"] == revision


def telegram_goals(session, command, now, *, sent_at=None, update_id=0):
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
                message_order=(int((sent_at or now).timestamp()), update_id),
            )
        except (KeyError, ValueError):
            return "Не удалось выбрать цели. Используйте: /goals сон самочувствие бег мигрень. Можно выбрать часть списка; /goals нет — убрать все цели."
    value = preferences(session)
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
