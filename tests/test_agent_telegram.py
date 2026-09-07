import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from garmin_ai.agent import Interpretation, apply_command, interpret
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState, Event, TelegramUpdate
from garmin_ai.telegram import (
    DeliveryUncertain,
    deliver,
    owned_message,
    process_message,
    save_update,
)


def update(text="кофе в 11", uid=42, update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1788782400,
            "from": {"id": uid},
            "chat": {"id": uid, "type": "private"},
            "text": text,
        },
    }


class FakeProvider:
    def __init__(self, result):
        self.result = result

    def structured(self, instruction, prompt, schema):
        return self.result


def test_private_allowlist_and_inbox_dedup(db):
    assert not save_update(db, update(uid=9), 42)
    assert save_update(db, update(), 42)
    assert save_update(db, update(), 42)
    assert db.scalar(select(func.count()).select_from(TelegramUpdate)) == 1
    group = update()
    group["message"]["chat"]["type"] = "group"
    assert owned_message(group, 42) is None


def test_telegram_retry_does_not_repeat_diary_mutation(db, db_engine):
    settings = Settings(telegram_user_id=42)
    save_update(db, update(), 42)
    db.commit()
    event = EventInput(
        start="2026-09-07T11:00:00+02:00", payload={"type": "caffeine", "beverage": "espresso"}
    )
    provider = FakeProvider(Interpretation(intent="log", events=[event], confidence=0.99))
    first = process_message(db_engine, provider, settings, 1)
    second = process_message(db_engine, provider, settings, 1)
    assert first == second
    assert db.scalar(select(func.count()).select_from(Event)) == 1


def test_clarification_and_future_event_never_write(db):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    command = Interpretation(intent="clarify", confidence=0.2, clarification="Какое лекарство?")
    assert (
        apply_command(db, command, text="таблетка 50 мг", update_id=1, actor="owner", now=now)
        == "Какое лекарство?"
    )
    assert db.scalar(select(func.count()).select_from(Event)) == 0
    assert db.get(AppState, "conversation:pending")
    event = EventInput(start="2027-01-01T00:00:00Z", payload={"type": "migraine"})
    result = interpret(
        db,
        FakeProvider(Interpretation(intent="log", events=[event], confidence=1)),
        "мигрень",
        Settings(),
        now,
    )
    assert result.intent == "clarify"


def test_close_preserves_original_episode_fields(db):
    original = EventInput(
        start="2026-09-07T10:00:00Z", payload={"type": "migraine", "severity": 6, "aura": False}
    )
    row = create_event(db, original, actor="owner")
    # Even a model output accidentally changing other fields cannot corrupt close.
    changed = EventInput(
        start="2026-09-07T11:00:00Z",
        end="2026-09-07T12:00:00Z",
        payload={"type": "migraine", "severity": 1},
    )
    command = Interpretation(intent="close", events=[changed], target_event_id=row.id, confidence=1)
    apply_command(
        db,
        command,
        text="закончилась",
        update_id=1,
        actor="owner",
        now=datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    assert row.start == original.start and row.payload["severity"] == 6 and row.end is not None


def test_ambiguous_telegram_delivery_is_not_repeated(db, db_engine):
    class Bot:
        calls = 0

        async def send_message(self, **kwargs):
            self.calls += 1
            raise TimeoutError("ambiguous response")

    bot = Bot()
    with pytest.raises(DeliveryUncertain):
        asyncio.run(deliver(bot, db_engine, 42, "test", "hello"))
    with pytest.raises(DeliveryUncertain):
        asyncio.run(deliver(bot, db_engine, 42, "test", "hello"))
    assert bot.calls == 1
