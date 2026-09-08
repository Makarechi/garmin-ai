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


def test_correction_changes_only_named_fields_and_can_add_event(db):
    original = EventInput(
        start="2026-09-07T10:00:00Z",
        payload={"type": "migraine", "severity": 6, "aura": False, "symptoms": ["synthetic"]},
    )
    row = create_event(db, original, actor="owner")
    changed = EventInput(start="2026-09-07T11:00:00Z", payload={"type": "migraine", "severity": 3})
    medication = EventInput(
        start="2026-09-07T11:00:00Z",
        payload={
            "type": "medication",
            "name": "synthetic",
            "dose": 50,
            "unit": "mg",
            "reason_event_id": row.id,
        },
    )
    command = Interpretation(
        intent="update",
        events=[changed, medication],
        target_event_id=row.id,
        changed_fields=["payload.severity"],
        confidence=1,
    )
    apply_command(db, command, text="synthetic", update_id=1, actor="owner", now=datetime.now(UTC))
    assert row.start == original.start and row.payload["symptoms"] == ["synthetic"]
    assert row.payload["severity"] == 3 and row.payload["aura"] is False
    assert db.scalar(select(func.count()).select_from(Event)) == 2


def test_emergency_response_does_not_require_evidence(db):
    from garmin_ai.agent import AgentStep, answer_question

    response = answer_question(
        db, FakeProvider(AgentStep(urgent_safety=True)), "synthetic", Settings(), datetime.now(UTC)
    )
    assert "112" in response


def test_delayed_message_uses_sent_time_and_empty_undo_replies(db, db_engine):
    import json

    class CapturingProvider:
        def structured(self, instruction, prompt, schema):
            assert datetime.fromisoformat(json.loads(prompt)["now"]) == datetime.fromtimestamp(
                1788782400, UTC
            )
            return Interpretation(intent="clarify", confidence=1, clarification="details?")

    settings = Settings(telegram_user_id=42)
    save_update(db, update(), 42)
    save_update(db, update("/undo", update_id=2), 42)
    db.commit()
    assert process_message(db_engine, CapturingProvider(), settings, 1) == "details?"
    assert process_message(db_engine, None, settings, 2)
    db.expire_all()
    assert db.get(TelegramUpdate, 2).status == "invalid"


def test_declared_emergency_routes_before_mutation(db, db_engine):
    save_update(db, update("synthetic emergency"), 42)
    db.commit()
    response = process_message(
        db_engine,
        FakeProvider(Interpretation(intent="safety", confidence=1)),
        Settings(telegram_user_id=42),
        1,
    )
    assert "112" in response and db.scalar(select(func.count()).select_from(Event)) == 0


def test_failed_tool_is_not_evidence(db):
    from garmin_ai.agent import AgentStep, ReadCall, answer_question

    class SequenceProvider:
        responses = iter(
            [
                AgentStep(calls=[ReadCall(name="invalid", arguments_json="{}")]),
                AgentStep(answer="unsupported", evidence_ids=[1]),
            ]
        )

        def structured(self, *args):
            return next(self.responses)

    response = answer_question(db, SequenceProvider(), "synthetic", Settings(), datetime.now(UTC))
    assert "unsupported" not in response


def test_rate_limited_send_remains_retryable(db, db_engine):
    from telegram.error import RetryAfter

    class Bot:
        async def send_message(self, **kwargs):
            raise RetryAfter(60)

    with pytest.raises(RetryAfter):
        asyncio.run(deliver(Bot(), db_engine, 42, "limited", "synthetic"))
    assert db.get(AppState, "outbox:limited:0").value["status"] == "pending"


def test_low_confidence_safety_is_preserved(db):
    command = interpret(
        db,
        FakeProvider(Interpretation(intent="safety", confidence=0.2)),
        "synthetic",
        Settings(),
        datetime.now(UTC),
    )
    assert command.intent == "safety"


def test_delayed_diary_job_blocks_later_diary_but_not_controls(db):
    from datetime import timedelta

    from garmin_ai.jobs import claim, enqueue

    now = datetime.now(UTC)
    enqueue(db, "telegram_update", {"update_id": 1}, "first", now + timedelta(minutes=5))
    enqueue(db, "telegram_update", {"update_id": 2}, "second", now)
    control = enqueue(db, "telegram_control", {"update_id": 3}, "control", now)
    assert claim(db, kinds=["telegram_update", "telegram_control"], now=now).id == control
    assert claim(db, kinds=["telegram_update"], now=now) is None
    assert (
        claim(db, kinds=["telegram_update"], now=now + timedelta(minutes=6)).payload["update_id"]
        == 1
    )


def test_safety_wins_over_extraneous_future_event(db):
    result = interpret(
        db,
        FakeProvider(
            Interpretation(
                intent="safety",
                confidence=0.2,
                events=[EventInput(start="2027-01-01T00:00:00Z", payload={"type": "migraine"})],
            )
        ),
        "опасный симптом",
        Settings(),
        datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert result.intent == "safety" and not result.events


def test_large_context_fails_safely_and_keeps_all_open_targets(db):
    from garmin_ai.agent import context_for

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    old = create_event(
        db, EventInput(start="2025-01-01T12:00:00Z", payload={"type": "migraine"}), actor="owner"
    )
    for _ in range(12):
        create_event(
            db,
            EventInput(
                start=now,
                original_text="x" * 16000,
                payload={"type": "note", "description": "z" * 3000},
            ),
            actor="owner",
        )
    assert str(old.id) in {r["id"] for r in context_for(db, now)["recent_events"]}

    class Capturing:
        def structured(self, instruction, prompt, schema):
            import json

            value = json.loads(prompt)
            assert value["text"] == "кофе сейчас" and len(prompt) < 24000
            return Interpretation(
                intent="log",
                confidence=1,
                events=[EventInput(start=now, payload={"type": "caffeine", "beverage": "coffee"})],
            )

    assert interpret(db, Capturing(), "кофе сейчас", Settings(), now).intent == "log"


def test_unknown_command_cannot_enable_questions_and_voice_unavailable(db, db_engine):
    settings = Settings(telegram_user_id=42)
    save_update(db, update("/resume_training"), 42)
    voice = update("", update_id=2)
    voice["message"]["voice"] = {"file_id": "synthetic"}
    save_update(db, voice, 42)
    db.commit()
    assert "Неизвестная" in process_message(db_engine, None, settings, 1)
    assert db.get(AppState, "proactive:enabled") is None
    assert "Gemini" in process_message(db_engine, None, settings, 2, "")


def test_multiple_clarifications_retain_all_answers(db):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    command = Interpretation(intent="clarify", confidence=0.5, clarification="уточните")
    for i, text in enumerate(["таблетка 50 мг", "суматриптан", "в 12"]):
        apply_command(db, command, text=text, update_id=i, actor="owner", now=now)
        db.flush()
        db.expire_all()
    pending = db.get(AppState, "conversation:pending").value
    assert [m["text"] for m in pending["messages"]] == ["таблетка 50 мг", "суматриптан", "в 12"]


def test_webhook_rejects_non_ascii_secret(db_engine):
    from fastapi.testclient import TestClient

    from garmin_ai.api import create_app

    settings = Settings(
        telegram_webhook_secret="synthetic-webhook-secret-32-characters", telegram_user_id=42
    )
    client = TestClient(create_app(settings, db_engine))
    response = client.post(
        "/telegram/webhook", headers=[(b"X-Telegram-Bot-Api-Secret-Token", b"\xff")], json={}
    )
    assert response.status_code == 403
