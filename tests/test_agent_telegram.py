import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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
                events=[
                    EventInput(
                        start=now,
                        timezone="UTC",
                        payload={"type": "caffeine", "beverage": "coffee"},
                    )
                ],
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


@pytest.mark.parametrize("button", ["coffee", "migraine", "alcohol", "medication", "note"])
def test_button_followup_replaces_obsolete_context(db, button):
    from garmin_ai.telegram import handle_button

    db.add(AppState(key="conversation:pending", value={"text": "obsolete"}))
    db.flush()
    response = handle_button(
        db, button, Settings(), "owner", 1, datetime(2026, 9, 7, 12, tzinfo=UTC)
    )
    db.expire_all()
    pending = db.get(AppState, "conversation:pending").value
    assert pending["question"] == response and pending["button"] == button
    assert "obsolete" not in str(pending)
    if button in {"coffee", "migraine", "alcohol"}:
        assert pending["action"] == "update" and len(pending["event_ids"]) == 1
    else:
        assert pending["action"] == "log" and pending["event_ids"] == []


def test_end_button_without_open_episode_exits_clarification(db):
    from garmin_ai.telegram import handle_button

    db.add(AppState(key="conversation:pending", value={"text": "obsolete"}))
    db.flush()
    response = handle_button(
        db, "end", Settings(), "owner", 1, datetime(2026, 9, 7, 12, tzinfo=UTC)
    )
    assert "Открытой мигрени нет" in response
    assert db.get(AppState, "conversation:pending") is None


def test_button_refinement_metadata_survives_clarification_and_expires(db):
    from datetime import timedelta

    from garmin_ai.agent import context_for
    from garmin_ai.telegram import handle_button

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    handle_button(db, "migraine", Settings(), "owner", 800, now)
    before = dict(db.get(AppState, "conversation:pending", populate_existing=True).value)
    apply_command(
        db,
        Interpretation(intent="clarify", confidence=0.5, clarification="Какая сила?"),
        text="сильная",
        update_id=801,
        actor="owner",
        now=now + timedelta(minutes=1),
    )
    value = context_for(db, now + timedelta(minutes=2))["pending_clarification"]
    assert all(value[key] == before[key] for key in ("event_ids", "action", "button"))
    assert context_for(db, now + timedelta(days=3))["pending_clarification"] is None


def test_direct_undo_clears_button_context(db, db_engine):
    from garmin_ai.telegram import handle_button

    handle_button(db, "coffee", Settings(), "telegram:42", 800, datetime.now(UTC))
    save_update(db, update("/undo"), 42)
    db.commit()
    assert "отменено" in process_message(db_engine, None, Settings(telegram_user_id=42), 1)
    db.expire_all()
    assert db.get(AppState, "conversation:pending") is None
    assert db.scalar(select(func.count()).select_from(Event).where(Event.deleted.is_(False))) == 0


def test_provider_calls_release_database_transactions(db, db_engine, monkeypatch):
    from sqlalchemy.orm import Session

    import garmin_ai.telegram as telegram_module
    from garmin_ai.agent import AgentStep, ReadCall

    sessions = []

    def capture_session(*args, **kwargs):
        session = Session(*args, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(telegram_module, "Session", capture_session)

    class Provider:
        count = 0

        def structured(self, instruction, prompt, schema):
            assert not sessions[-1].in_transaction()
            self.count += 1
            if schema is Interpretation:
                return Interpretation(intent="question", confidence=1)
            if self.count == 2:
                return AgentStep(calls=[ReadCall(name="data_freshness", arguments_json="{}")])
            return AgentStep(answer="Данных пока нет.", evidence_ids=[1])

    save_update(db, update("Какие данные доступны?"), 42)
    db.commit()
    provider = Provider()
    assert "Данных пока нет" in process_message(
        db_engine, provider, Settings(telegram_user_id=42), 1
    )
    assert provider.count == 3


def test_voice_without_declared_size_is_rejected_before_transcription():
    from garmin_ai.runtime import VoiceTooLarge, transcribe_voice

    class File:
        async def download_as_bytearray(self):
            return bytearray(20 * 1024 * 1024 + 1)

    class Bot:
        async def get_file(self, file_id):
            return File()

    class Provider:
        def transcribe(self, *args):
            pytest.fail("Oversized audio must not reach the provider")

    with pytest.raises(VoiceTooLarge):
        asyncio.run(transcribe_voice(Bot(), Provider(), {"file_id": "synthetic"}))


def test_backlogged_button_and_text_use_processing_clock_for_clarification(db, db_engine):
    import json

    settings = Settings(telegram_user_id=42)
    callback = {
        "update_id": 901,
        "callback_query": {
            "id": "synthetic",
            "from": {"id": 42},
            "data": "migraine",
            "message": update()["message"],
        },
    }
    save_update(db, callback, 42, callback_time_known=True)
    save_update(db, update("сильная", update_id=902), 42)
    db.commit()
    process_message(db_engine, None, settings, 901)

    class Provider:
        def structured(self, instruction, prompt, schema):
            pending = json.loads(prompt)["context"]["pending_clarification"]
            assert pending["action"] == "update" and pending["event_ids"]
            return Interpretation(intent="clarify", confidence=1, clarification="От 0 до 10?")

    assert process_message(db_engine, Provider(), settings, 902) == "От 0 до 10?"
    db.expire_all()
    assert db.get(AppState, "conversation:pending").value["button"] == "migraine"


def test_thinking_configuration_is_opt_in(monkeypatch):
    from garmin_ai.llm import GeminiProvider

    monkeypatch.delenv("GA_GEMINI_THINKING_LEVEL", raising=False)
    settings = Settings(
        _env_file=None, llm_enabled=True, gemini_api_key="synthetic", gemini_model="synthetic-model"
    )
    provider = GeminiProvider(settings)
    try:
        assert provider.generation_config == {}
    finally:
        provider.close()


def test_refinement_validates_only_changed_timestamps(db):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    saved = create_event(
        db, EventInput(start="2026-09-08T12:00:00Z", payload={"type": "migraine"}), actor="owner"
    )
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "created_at": now.isoformat(),
                "event_ids": [str(saved.id)],
                "action": "update",
                "button": "migraine",
            },
        )
    )
    db.flush()
    proposed = EventInput(start=saved.start, payload={"type": "migraine", "severity": 7})
    command = Interpretation(
        intent="update",
        confidence=1,
        events=[proposed],
        target_event_id=saved.id,
        changed_fields=["payload.severity"],
    )
    assert interpret(db, FakeProvider(command), "боль 7", Settings(), now).intent == "update"
    command.changed_fields = ["start"]
    assert (
        interpret(db, FakeProvider(command), "началась завтра", Settings(), now).intent == "clarify"
    )


def test_history_cannot_overtake_pending_diary_and_terminal_failure_notifies_once(db):
    from datetime import timedelta

    from garmin_ai.jobs import claim
    from garmin_ai.models import Job
    from garmin_ai.telegram import reconcile_failed_inbox

    now = datetime.now(UTC)
    save_update(db, update("кофе", update_id=1), 42)
    save_update(db, update("/history", update_id=2), 42)
    first = db.scalar(select(Job).where(Job.payload["update_id"].astext == "1"))
    first.run_at = now + timedelta(hours=1)
    db.flush()
    assert claim(db, now=now, kinds=["telegram_update", "telegram_control"]) is None
    first.status = "failed"
    db.flush()
    reconcile_failed_inbox(db)
    reconcile_failed_inbox(db)
    assert (
        db.scalar(select(func.count()).select_from(Job).where(Job.kind == "telegram_failure")) == 1
    )
    assert (
        claim(db, now=now + timedelta(seconds=1), kinds=["telegram_update"]).payload["update_id"]
        == 2
    )


def test_shutdown_drains_native_work_before_returning():
    import threading

    from garmin_ai.runtime import drain_workers, run_blocking

    entered, release = threading.Event(), threading.Event()
    completed = []

    def work():
        entered.set()
        release.wait(timeout=5)
        completed.append(True)

    async def check():
        task = asyncio.create_task(run_blocking(work))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        drain = asyncio.create_task(drain_workers([task]))
        await asyncio.sleep(0.01)
        assert not drain.done()
        release.set()
        await drain
        assert completed == [True]

    asyncio.run(check())


@pytest.mark.parametrize("intent", ["log", "update"])
def test_button_refinement_cannot_create_duplicate_or_edit_other_record(db, intent):
    from garmin_ai.telegram import handle_button

    now = datetime.now(UTC)
    other = create_event(db, EventInput(start=now, payload={"type": "migraine"}), actor="owner")
    handle_button(db, "migraine", Settings(), "owner", 100, now)
    command = Interpretation(
        intent=intent,
        confidence=1,
        events=[EventInput(start=now, payload={"type": "migraine", "severity": 7})],
        target_event_id=other.id if intent == "update" else None,
        changed_fields=["payload.severity"],
    )
    assert interpret(db, FakeProvider(command), "боль 7", Settings(), now).intent == "clarify"


def test_unknown_callback_time_requires_confirmation_and_voice_keeps_caption(db, db_engine):
    import json

    from garmin_ai.telegram import handle_button

    now = datetime.now(UTC)
    assert "время" in handle_button(db, "coffee", Settings(), "owner", 700, now, time_known=False)
    assert db.scalar(select(func.count()).select_from(Event)) == 0
    voice = update("", update_id=701)
    voice["message"].update(voice={"file_id": "synthetic"}, caption="синтетическое название, 50 мг")
    save_update(db, voice, 42)
    db.commit()

    class Provider:
        def structured(self, instruction, prompt, schema):
            text = json.loads(prompt)["text"]
            assert "принял в 12" in text and "50 мг" in text
            return Interpretation(intent="clarify", confidence=1, clarification="Уточните дату")

    assert (
        process_message(db_engine, Provider(), Settings(telegram_user_id=42), 701, "принял в 12")
        == "Уточните дату"
    )


def test_exhausted_partial_reply_requeues_delivery_without_mutation(db):
    from garmin_ai.models import Job
    from garmin_ai.telegram import reconcile_failed_inbox

    save_update(db, update(), 42)
    db.get(TelegramUpdate, 1).status = "processed"
    job = db.scalar(select(Job))
    job.status, job.attempts = "failed", 8
    db.add(AppState(key="telegram:reply:1", value={"text": "x" * 4000, "status": "pending"}))
    db.add(AppState(key="outbox:update:1:0", value={"status": "sent"}))
    db.add(AppState(key="outbox:update:1:3500", value={"status": "pending"}))
    db.flush()
    reconcile_failed_inbox(db)
    assert job.status == "pending" and job.attempts == 0
    assert db.get(TelegramUpdate, 1).status == "processed"


def test_known_delivery_rate_limit_does_not_exhaust_retry_budget(db):
    from garmin_ai.jobs import claim, enqueue, finish

    now = datetime.now(UTC)
    enqueue(db, "telegram_control", {}, "synthetic-send", now)
    job = claim(db, now=now)
    job.attempts = 8
    db.flush()
    finish(db, job.id, job.lease_token, error_type="RetryAfter", retryable_delivery=True)
    assert job.status == "pending" and job.attempts == 7


@pytest.mark.parametrize("intent,kind", [("update", "medication"), ("log", "migraine")])
def test_pending_log_button_constrains_action_and_event_kind(db, intent, kind):
    from garmin_ai.telegram import handle_button

    now = datetime.now(UTC)
    target = create_event(db, EventInput(start=now, payload={"type": "migraine"}), actor="owner")
    handle_button(db, "medication", Settings(), "owner", 800, now)
    payload = (
        {"type": "medication", "name": "synthetic", "dose": 1, "unit": "mg"}
        if kind == "medication"
        else {"type": "migraine"}
    )
    command = Interpretation(
        intent=intent,
        confidence=1,
        events=[EventInput(start=now, payload=payload)],
        target_event_id=target.id if intent == "update" else None,
    )
    assert interpret(db, FakeProvider(command), "synthetic", Settings(), now).intent == "clarify"


@pytest.mark.parametrize("urgent", [False, True])
def test_stalled_diary_allows_safety_check_without_reordering_mutations(db, db_engine, urgent):
    from datetime import timedelta

    from garmin_ai.jobs import claim
    from garmin_ai.models import Job
    from garmin_ai.telegram import DiaryDeferred

    now = datetime.now(UTC)
    save_update(db, update("кофе", update_id=1), 42)
    save_update(db, update("внезапные тяжёлые симптомы", update_id=2), 42)
    older = db.scalar(select(Job).where(Job.dedup_key == "telegram:1"))
    older.run_at = now + timedelta(hours=1)
    db.flush()
    assert claim(db, now=now + timedelta(seconds=1)).payload["update_id"] == 2
    db.commit()
    command = Interpretation(intent="safety" if urgent else "clarify", confidence=1)
    if urgent:
        assert "112" in process_message(
            db_engine, FakeProvider(command), Settings(telegram_user_id=42), 2
        )
    else:
        with pytest.raises(DiaryDeferred):
            process_message(db_engine, FakeProvider(command), Settings(telegram_user_id=42), 2)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Event)) == 0
    assert db.get(TelegramUpdate, 1).status == "pending"
    assert db.get(TelegramUpdate, 2).status == ("processed" if urgent else "pending")


@pytest.mark.parametrize("episodes", [1, 2])
@pytest.mark.parametrize("intent", ["log", "update", "close"])
def test_end_clarification_requires_closing_candidate(db, episodes, intent):
    from datetime import timedelta

    from garmin_ai.telegram import handle_button

    now = datetime.now(UTC)
    rows = [
        create_event(
            db,
            EventInput(start=now - timedelta(hours=i + 1), payload={"type": "migraine"}),
            actor="owner",
        )
        for i in range(episodes)
    ]
    handle_button(db, "end", Settings(), "owner", 999, now, time_known=False)
    db.expire_all()
    pending = db.get(AppState, "conversation:pending").value
    assert pending["action"] == "close" and pending["button"] == "end"
    command = Interpretation(
        intent=intent,
        confidence=1,
        target_event_id=rows[0].id if intent != "log" else None,
        events=[
            EventInput(
                start=rows[0].start,
                end=now.astimezone(ZoneInfo(rows[0].timezone)),
                timezone=rows[0].timezone,
                payload={"type": "migraine", "severity": 5},
            )
        ],
        changed_fields=["payload.severity"],
    )
    result = interpret(db, FakeProvider(command), "первый закончился сейчас", Settings(), now)
    assert result.intent == ("close" if intent == "close" else "clarify")


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("sent", [False, True])
def test_undo_close_restores_linked_question(db, db_engine, direct, sent):
    from datetime import timedelta

    from garmin_ai.models import PendingQuestion

    now = datetime.now(UTC)
    event = EventInput(start=now - timedelta(hours=2), payload={"type": "migraine"})
    row = create_event(db, event, actor="telegram:42")
    question = PendingQuestion(
        kind="migraine",
        event_id=row.id,
        text="Закончилась?",
        evidence={},
        priority=1,
        earliest_send_at=now,
        expires_at=now + timedelta(days=1),
        sent_at=now if sent else None,
        status="sent" if sent else "pending",
        dedup_key="synthetic",
    )
    db.add(question)
    db.flush()
    identity = question.id
    apply_command(
        db,
        Interpretation(
            intent="close",
            confidence=1,
            target_event_id=row.id,
            events=[event.model_copy(update={"end": now})],
        ),
        text="закончилась",
        update_id=100,
        actor="telegram:42",
        now=now,
    )
    assert question.status == "answered"
    if direct:
        save_update(db, update("/undo", update_id=101), 42)
        db.commit()
        process_message(db_engine, None, Settings(telegram_user_id=42), 101)
        db.expire_all()
    else:
        apply_command(
            db,
            Interpretation(intent="undo", confidence=1),
            text="отмени",
            update_id=101,
            actor="telegram:42",
            now=now,
        )
    assert db.get(PendingQuestion, identity).status == ("sent" if sent else "pending")
    assert db.get(Event, row.id).end is None


@pytest.mark.parametrize("latest", ["/pause", "/resume"])
def test_proactive_commands_respect_send_order_when_processed_backwards(db, db_engine, latest):
    previous = "/resume" if latest == "/pause" else "/pause"
    save_update(db, update(previous, update_id=100), 42)
    save_update(db, update(latest, update_id=101), 42)
    db.commit()
    settings = Settings(telegram_user_id=42)
    process_message(db_engine, None, settings, 101)
    response = process_message(db_engine, None, settings, 100)
    db.expire_all()
    assert db.get(AppState, "proactive:enabled").value == {
        "enabled": latest == "/resume",
        "update_id": 101,
        "message_at": 1788782400,
    }
    assert ("вопросы разрешены" if latest == "/resume" else "Вопросы отключены") in response


def test_pause_accepts_newer_message_after_update_id_reset(db, db_engine):
    db.add(
        AppState(
            key="proactive:enabled",
            value={"enabled": True, "message_at": 1788782300, "update_id": 9999999},
        )
    )
    save_update(db, update("/pause", update_id=10), 42)
    db.commit()
    process_message(db_engine, None, Settings(telegram_user_id=42), 10)
    db.expire_all()
    assert db.get(AppState, "proactive:enabled").value["enabled"] is False


@pytest.mark.parametrize(
    "stamp,valid",
    [
        ("2026-07-01T11:00:00+01:00", False),
        ("2026-07-01T11:00:00+02:00", True),
        ("2026-01-01T11:00:00+02:00", False),
        ("2026-01-01T11:00:00+01:00", True),
        ("2026-03-29T02:30:00+01:00", False),
    ],
)
def test_interpreter_validates_local_timezone_offset(db, stamp, valid):
    command = Interpretation(
        intent="log",
        confidence=1,
        events=[
            EventInput(
                start=stamp,
                timezone="Europe/Bratislava",
                payload={"type": "note", "description": "synthetic"},
            )
        ],
    )
    result = interpret(
        db,
        FakeProvider(command),
        "заметка в указанное время",
        Settings(),
        datetime(2026, 9, 7, tzinfo=UTC),
    )
    assert result.intent == ("log" if valid else "clarify")


def test_initial_event_limit_blocks_ambiguous_correction(db):
    from datetime import timedelta

    from garmin_ai.agent import context_for

    now = datetime.now(UTC)
    rows = [
        create_event(
            db,
            EventInput(
                start=now - timedelta(minutes=i), payload={"type": "note", "description": str(i)}
            ),
            actor="owner",
        )
        for i in range(13)
    ]
    assert context_for(db, now)["history_truncated"] is True
    command = Interpretation(
        intent="update",
        confidence=1,
        target_event_id=rows[0].id,
        events=[EventInput(start=now, payload={"type": "note", "description": "changed"})],
        changed_fields=["payload.description"],
    )
    assert (
        interpret(db, FakeProvider(command), "исправь прежнюю заметку", Settings(), now).intent
        == "clarify"
    )
    assert (
        interpret(db, FakeProvider(command), f"исправь {rows[0].id}", Settings(), now).intent
        == "update"
    )


def test_ordinary_correction_reopens_followup(db):
    from datetime import timedelta

    from garmin_ai.events import update_event
    from garmin_ai.models import PendingQuestion

    now = datetime.now(UTC)
    event = EventInput(start=now - timedelta(hours=2), end=now, payload={"type": "migraine"})
    row = create_event(db, event, actor="owner")
    q = PendingQuestion(
        kind="migraine",
        event_id=row.id,
        text="test",
        evidence={},
        priority=1,
        earliest_send_at=now,
        expires_at=now + timedelta(days=1),
        sent_at=now,
        status="answered",
        dedup_key="synthetic-reopen",
    )
    db.add(q)
    db.flush()
    update_event(
        db, row.id, event.model_copy(update={"end": None}), revision=row.revision, actor="owner"
    )
    assert q.status == "sent"


def test_voice_transcript_survives_retry(db, db_engine, monkeypatch):
    from garmin_ai.runtime import cached_transcription

    calls = []

    async def transcribe(*args):
        calls.append(True)
        return "synthetic transcription"

    monkeypatch.setattr("garmin_ai.runtime.transcribe_voice", transcribe)

    async def check():
        first = await cached_transcription(db_engine, None, None, {}, 777)
        second = await cached_transcription(db_engine, None, None, {}, 777)
        assert first == second == "synthetic transcription"

    asyncio.run(check())
    assert calls == [True]
    assert db.get(AppState, "telegram:transcript:777").value["text"] == "synthetic transcription"


def test_callback_ack_is_claimable_while_diary_is_deferred(db):
    from datetime import timedelta

    from garmin_ai.jobs import claim
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    save_update(db, update(update_id=1), 42)
    older = db.scalar(select(Job).where(Job.dedup_key == "telegram:1"))
    older.run_at = now + timedelta(hours=1)
    callback = {
        "update_id": 2,
        "callback_query": {
            "id": "synthetic-callback",
            "from": {"id": 42},
            "data": "coffee",
            "message": update()["message"],
        },
    }
    save_update(db, callback, 42)
    assert claim(db, kinds=["telegram_update"], now=now + timedelta(seconds=1)) is None
    acknowledgement = claim(db, kinds=["telegram_ack"], now=now + timedelta(seconds=1))
    assert acknowledgement.payload["update_id"] == 2
    assert db.get(TelegramUpdate, 2).status == "pending"


def test_delayed_context_excludes_later_events_but_keeps_explicit_button_target(db):
    from datetime import timedelta

    from garmin_ai.agent import context_for

    now = datetime.now(UTC)
    later = create_event(
        db, EventInput(start=now + timedelta(hours=1), payload={"type": "migraine"}), actor="owner"
    )
    assert str(later.id) not in {r["id"] for r in context_for(db, now)["recent_events"]}
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "created_at": now.isoformat(),
                "event_ids": [str(later.id)],
                "action": "update",
                "button": "migraine",
            },
        )
    )
    db.flush()
    assert str(later.id) in {r["id"] for r in context_for(db, now)["recent_events"]}


def test_model_correction_rejects_concurrent_revision(db, db_engine):
    from garmin_ai.db import transaction
    from garmin_ai.events import Conflict, update_event

    now = datetime.now(UTC)
    event = EventInput(start=now, payload={"type": "migraine", "severity": 3})
    row = create_event(db, event, actor="owner")
    identity = row.id
    db.commit()
    command = Interpretation(
        intent="update",
        confidence=1,
        target_event_id=identity,
        events=[EventInput(start=now, payload={"type": "migraine", "severity": 4})],
        changed_fields=["payload.severity"],
    )

    class ConcurrentProvider:
        def structured(self, *args):
            with transaction(db_engine) as other:
                target = other.get(Event, identity)
                update_event(
                    other,
                    identity,
                    EventInput(start=now, payload={"type": "migraine", "severity": 7}),
                    revision=target.revision,
                    actor="api",
                )
            return command

    result = interpret(
        db, ConcurrentProvider(), "сила четыре", Settings(), now, before_model=db.commit
    )
    with pytest.raises(Conflict):
        apply_command(db, result, text="сила четыре", update_id=777, actor="owner", now=now)
    db.rollback()
    db.expire_all()
    assert db.get(Event, identity).payload["severity"] == 7


def test_ingress_transaction_blocks_diary_claim_until_publication(db, db_engine):
    from sqlalchemy import text

    from garmin_ai.db import transaction
    from garmin_ai.jobs import claim

    save_update(db, update("/undo", update_id=2), 42)
    db.commit()
    with transaction(db_engine) as incoming:
        incoming.execute(text("SELECT pg_advisory_xact_lock(72104623)"))
        assert claim(db, kinds=["telegram_update"]) is None
        db.commit()
        save_update(incoming, update(update_id=1), 42)
    job = claim(db, kinds=["telegram_update"])
    assert job.payload["update_id"] == 1


def test_webhook_uses_single_connection_without_dropping_pending_updates():
    from types import SimpleNamespace

    from garmin_ai.runtime import serialize_webhook_delivery

    recorded = []

    class FakeBot:
        async def set_webhook(self, **kwargs):
            recorded.append(kwargs)

    asyncio.run(
        serialize_webhook_delivery(
            FakeBot(),
            SimpleNamespace(url="https://example.invalid/hook"),
            Settings(telegram_webhook_secret="synthetic-secret-32-characters"),
        )
    )
    assert recorded[0]["max_connections"] == 1 and recorded[0]["drop_pending_updates"] is False


@pytest.mark.parametrize("button,intent", [("coffee", "update"), ("end", "close")])
def test_exact_button_target_can_be_refined_in_large_diary(db, button, intent):
    from datetime import timedelta

    from garmin_ai.telegram import handle_button

    now = datetime.now(UTC)
    if button == "coffee":
        handle_button(
            db, "coffee", Settings(timezone="UTC"), "owner", 100, now - timedelta(hours=1)
        )
    else:
        create_event(
            db,
            EventInput(
                start=now - timedelta(hours=1), timezone="UTC", payload={"type": "migraine"}
            ),
            actor="owner",
        )
        handle_button(db, "end", Settings(timezone="UTC"), "owner", 100, now, time_known=False)
    for i in range(14):
        create_event(
            db,
            EventInput(start=now, timezone="UTC", payload={"type": "note", "description": str(i)}),
            actor="owner",
        )
    pending = db.get(AppState, "conversation:pending", populate_existing=True).value
    from uuid import UUID

    row = db.get(Event, UUID(pending["event_ids"][0]))
    proposed = EventInput(
        start=row.start,
        end=now if intent == "close" else None,
        timezone="UTC",
        payload={"type": "migraine"}
        if intent == "close"
        else {"type": "caffeine", "beverage": "espresso"},
    )
    command = Interpretation(
        intent=intent,
        confidence=1,
        target_event_id=row.id,
        events=[proposed],
        changed_fields=["end"] if intent == "close" else ["payload.beverage"],
    )
    result = interpret(
        db, FakeProvider(command), "уточняю выбранную запись", Settings(timezone="UTC"), now
    )
    assert result.intent == intent
    apply_command(db, result, text="synthetic", update_id=101, actor="owner", now=now)
    assert row.end == now if intent == "close" else row.payload["beverage"] == "espresso"


@pytest.mark.parametrize("current", [False, True])
def test_end_button_ignores_future_migraine(db, current):
    from datetime import timedelta

    from garmin_ai.telegram import handle_button

    now = datetime.now(UTC)
    future = create_event(
        db, EventInput(start=now + timedelta(days=1), payload={"type": "migraine"}), actor="owner"
    )
    if current:
        active = create_event(
            db,
            EventInput(start=now - timedelta(hours=1), payload={"type": "migraine"}),
            actor="owner",
        )
    response = handle_button(db, "end", Settings(), "owner", 100, now)
    assert future.end is None
    if current:
        assert active.end == now and "завершение" in response
    else:
        assert "Открытой мигрени нет" in response


@pytest.mark.parametrize("complete", [False, True])
def test_multiple_end_candidates_can_be_selected_in_large_diary(db, complete):
    from datetime import timedelta

    from garmin_ai.telegram import handle_button

    now = datetime.now(UTC)
    episodes = [
        create_event(
            db,
            EventInput(
                start=now - timedelta(hours=i + 1), timezone="UTC", payload={"type": "migraine"}
            ),
            actor="owner",
        )
        for i in range(2)
    ]
    for i in range(14):
        create_event(
            db,
            EventInput(start=now, timezone="UTC", payload={"type": "note", "description": str(i)}),
            actor="owner",
        )
    handle_button(db, "end", Settings(timezone="UTC"), "owner", 100, now, time_known=False)
    pending = db.get(AppState, "conversation:pending", populate_existing=True)
    pending.value = {**pending.value, "targets_complete": complete}
    db.flush()
    apply_command(
        db,
        Interpretation(intent="clarify", confidence=0.5, clarification="какой?"),
        text="synthetic",
        update_id=101,
        actor="owner",
        now=now,
    )
    command = Interpretation(
        intent="close",
        confidence=1,
        target_event_id=episodes[1].id,
        events=[
            EventInput(
                start=episodes[1].start, end=now, timezone="UTC", payload={"type": "migraine"}
            )
        ],
    )
    result = interpret(
        db, FakeProvider(command), "второй закончился сейчас", Settings(timezone="UTC"), now
    )
    assert result.intent == ("close" if complete else "clarify")
    if complete:
        apply_command(db, result, text="synthetic", update_id=102, actor="owner", now=now)
        assert episodes[1].end == now and episodes[0].end is None


@pytest.mark.parametrize("sent", [False, True])
def test_end_button_answers_followup_and_undo_renews_expired_validity(db, sent):
    from datetime import timedelta

    from garmin_ai.events import undo_last
    from garmin_ai.models import PendingQuestion
    from garmin_ai.telegram import handle_button

    now = datetime.now(UTC)
    episode = create_event(
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
    )
    question = PendingQuestion(
        kind="migraine",
        event_id=episode.id,
        text="synthetic",
        evidence={},
        priority=1,
        earliest_send_at=now - timedelta(days=4),
        expires_at=now - timedelta(days=1),
        sent_at=now - timedelta(days=3) if sent else None,
        status="sent" if sent else "pending",
        dedup_key="synthetic-expired",
    )
    db.add(question)
    db.flush()
    handle_button(db, "end", Settings(), "owner", 100, now)
    assert question.status == "answered"
    undo_last(db, actor="owner")
    assert question.status == ("sent" if sent else "pending")
    assert question.expires_at > now + timedelta(days=1)
    assert question.sent_at == (now - timedelta(days=3) if sent else None)


def test_restored_deleted_migraine_renews_old_cancelled_question(db):
    from datetime import timedelta

    from garmin_ai.events import delete_event, undo_last
    from garmin_ai.models import PendingQuestion

    now = datetime.now(UTC)
    episode = create_event(
        db, EventInput(start=now - timedelta(days=12), payload={"type": "migraine"}), actor="owner"
    )
    q = PendingQuestion(
        kind="migraine",
        event_id=episode.id,
        text="synthetic",
        evidence={},
        priority=1,
        earliest_send_at=now - timedelta(days=12),
        expires_at=now - timedelta(days=10),
        sent_at=now - timedelta(days=12),
        status="cancelled",
        dedup_key="old-cancelled",
    )
    db.add(q)
    db.flush()
    delete_event(db, episode.id, revision=episode.revision, actor="owner")
    undo_last(db, actor="owner")
    assert q.status == "sent" and q.expires_at > now and not episode.deleted


@pytest.mark.parametrize("offset", ["+02:00", "+01:00"])
@pytest.mark.parametrize("explicit", [False, True])
def test_repeated_dst_hour_requires_user_offset(db, offset, explicit):
    command = Interpretation(
        intent="log",
        confidence=1,
        events=[
            EventInput(
                start="2026-10-25T02:30:00" + offset,
                timezone="Europe/Bratislava",
                payload={"type": "note", "description": "synthetic"},
            )
        ],
    )
    result = interpret(
        db,
        FakeProvider(command),
        "заметка 25 октября в 02:30" + (offset if explicit else ""),
        Settings(),
        datetime(2026, 10, 26, tzinfo=UTC),
    )
    assert result.intent == ("log" if explicit else "clarify")


@pytest.mark.parametrize("change_timezone", [False, True])
def test_correction_checks_retained_timezone(db, change_timezone):
    row = create_event(
        db,
        EventInput(
            start="2026-09-07T09:00:00Z",
            timezone="UTC",
            payload={"type": "note", "description": "synthetic"},
        ),
        actor="owner",
    )
    command = Interpretation(
        intent="update",
        confidence=1,
        target_event_id=row.id,
        changed_fields=["start"] + (["timezone"] if change_timezone else []),
        events=[
            EventInput(
                start="2026-09-07T11:30:00+02:00",
                timezone="Europe/Bratislava",
                payload={"type": "note", "description": "synthetic"},
            )
        ],
    )
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    result = interpret(db, FakeProvider(command), "исправь время на 11:30", Settings(), now)
    assert result.intent == ("update" if change_timezone else "clarify")
    if change_timezone:
        apply_command(db, result, text="synthetic", update_id=44, actor="owner", now=now)
        assert row.timezone == "Europe/Bratislava" and row.start.astimezone(UTC).hour == 9


@pytest.mark.parametrize("status", ["inferred", "needs_confirmation"])
def test_explicit_telegram_fact_is_confirmed(db, status):
    command = Interpretation(
        intent="log",
        confidence=1,
        events=[
            EventInput(
                start="2026-09-07T11:00:00+02:00",
                status=status,
                payload={"type": "note", "description": "synthetic"},
            )
        ],
    )
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    result = interpret(db, FakeProvider(command), "заметка в 11", Settings(), now)
    apply_command(db, result, text="synthetic", update_id=45, actor="owner", now=now)
    assert db.scalar(select(Event)).status == "confirmed"


@pytest.mark.parametrize("extra_calls", [False, True])
def test_final_agent_turn_synthesizes_without_executing_more_tools(db, monkeypatch, extra_calls):
    import json

    from garmin_ai import agent

    calls = []
    monkeypatch.setattr(agent, "call_tool", lambda *args: calls.append(args[1]) or {"value": 1})

    class Provider:
        turn = 0

        def structured(self, instruction, prompt, schema):
            payload = json.loads(prompt)
            self.turn += 1
            if self.turn <= 5:
                return agent.AgentStep(
                    calls=[agent.ReadCall(name="synthetic", arguments_json="{}")]
                )
            assert payload["answer_only"] and not payload["tools"] and len(payload["evidence"]) == 5
            if extra_calls:
                return agent.AgentStep(
                    calls=[agent.ReadCall(name="synthetic", arguments_json="{}")]
                )
            return agent.AgentStep(answer="Проверенный результат", evidence_ids=[5])

    response = agent.answer_question(db, Provider(), "synthetic", Settings(), datetime.now(UTC))
    assert len(calls) == 5
    assert ("Проверенный результат" in response) is not extra_calls


def test_diary_order_survives_idle_update_id_reset(db, db_engine):
    from datetime import timedelta

    from garmin_ai.jobs import claim
    from garmin_ai.models import Job
    from garmin_ai.telegram import DiaryDeferred

    now = datetime.now(UTC)
    save_update(db, update("/undo", update_id=900), 42)
    ordering = db.get(AppState, "telegram:ordering", populate_existing=True)
    ordering.value = {**ordering.value, "last_received_at": (now - timedelta(days=8)).isoformat()}
    db.flush()
    save_update(db, update("/undo", update_id=10), 42)
    old = db.scalar(select(Job).where(Job.dedup_key == "telegram:900"))
    old.run_at = now + timedelta(hours=1)
    db.commit()
    assert claim(db, now=now + timedelta(seconds=5), kinds=["telegram_update"]) is None
    db.rollback()
    with pytest.raises(DiaryDeferred):
        process_message(db_engine, None, Settings(telegram_user_id=42), 10)
    old = db.scalar(select(Job).where(Job.dedup_key == "telegram:900"))
    old.run_at = now
    db.flush()
    claimed = claim(db, now=now + timedelta(seconds=5), kinds=["telegram_update"])
    assert claimed.payload["update_id"] == 900


@pytest.mark.parametrize("old", [False, True])
def test_explicit_uuid_loads_event_omitted_from_recent_context(db, old):
    from datetime import timedelta

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    target = create_event(
        db,
        EventInput(
            start=now - timedelta(days=30 if old else 1),
            timezone="UTC",
            payload={"type": "note", "description": "before"},
        ),
        actor="owner",
    )
    for index in range(14):
        create_event(
            db,
            EventInput(
                start=now - timedelta(minutes=index),
                timezone="UTC",
                payload={"type": "note", "description": "other"},
            ),
            actor="owner",
        )
    proposed = EventInput(
        start=target.start, timezone="UTC", payload={"type": "note", "description": "after"}
    )
    command = Interpretation(
        intent="update",
        confidence=1,
        target_event_id=target.id,
        events=[proposed],
        changed_fields=["payload.description"],
    )
    result = interpret(
        db, FakeProvider(command), f"исправь запись {str(target.id).upper()}", Settings(), now
    )
    assert result.intent == "update"
    apply_command(db, result, text="synthetic", update_id=800, actor="owner", now=now)
    assert (
        target.payload["description"] == "after"
        and db.scalar(select(func.count()).select_from(Event)) == 15
    )


def test_explicit_unknown_uuid_requests_clarification(db):
    from uuid import uuid4

    class Provider:
        def structured(self, *args):
            pytest.fail("Unknown explicit target must not call provider")

    result = interpret(db, Provider(), f"исправь {uuid4()}", Settings(), datetime.now(UTC))
    assert result.intent == "clarify"


@pytest.mark.parametrize("same_time", [False, True])
@pytest.mark.parametrize("qualified", [False, True])
def test_each_ambiguous_timestamp_requires_its_own_offset(db, same_time, qualified):
    minute = "15" if same_time else "30"
    command = Interpretation(
        intent="log",
        confidence=1,
        events=[
            EventInput(
                start="2026-10-25T02:15:00+02:00",
                payload={"type": "caffeine", "beverage": "coffee"},
            ),
            EventInput(start=f"2026-10-25T02:{minute}:00+02:00", payload={"type": "migraine"}),
        ],
    )
    message = f"кофе 02:15+02:00, мигрень 02:{minute}" + ("+02:00" if qualified else "")
    result = interpret(
        db, FakeProvider(command), message, Settings(), datetime(2026, 10, 26, tzinfo=UTC)
    )
    assert result.intent == ("log" if qualified else "clarify")


@pytest.mark.parametrize("status", ["pending", "sent"])
def test_kind_change_retires_linked_migraine_questions(db, status):
    from datetime import timedelta

    from garmin_ai.events import undo_last, update_event
    from garmin_ai.models import PendingQuestion

    now = datetime.now(UTC)
    event = create_event(db, EventInput(start=now, payload={"type": "migraine"}), actor="owner")
    q = PendingQuestion(
        kind="migraine",
        event_id=event.id,
        text="synthetic",
        evidence={},
        priority=0.9,
        dedup_key="kind-change",
        earliest_send_at=now,
        expires_at=now + timedelta(days=2),
        status=status,
        sent_at=now if status == "sent" else None,
    )
    db.add(q)
    db.flush()
    update_event(
        db,
        event.id,
        EventInput(start=now, payload={"type": "note", "description": "synthetic"}),
        revision=1,
        actor="owner",
    )
    assert q.status == "cancelled"
    undo_last(db, actor="owner")
    assert q.status == status


def test_runtime_without_bot_leaves_telegram_work_queued(db, db_engine, tmp_path, monkeypatch):
    from garmin_ai import runtime
    from garmin_ai.models import Job

    save_update(db, update("/undo"), 42)
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="",
        telegram_user_id=42,
        llm_enabled=False,
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)

    async def check():
        loop = asyncio.get_running_loop()
        callbacks = []
        monkeypatch.setattr(
            loop, "add_signal_handler", lambda signal, callback: callbacks.append(callback)
        )
        task = asyncio.create_task(runtime.run(settings))
        await asyncio.sleep(0.15)
        assert not task.done() and callbacks
        callbacks[0]()
        await asyncio.wait_for(task, 3)

    asyncio.run(check())
    db.expire_all()
    job = db.scalar(select(Job).where(Job.dedup_key == "telegram:1"))
    assert job.status == "pending" and job.attempts == 0
    assert db.get(TelegramUpdate, 1).status == "pending"
    assert db.get(AppState, "runtime:heartbeat") is not None


@pytest.mark.parametrize("position", [0, 12000, 22000])
def test_long_voice_transcript_screens_all_bounded_fragments(db, db_engine, position):
    from garmin_ai.agent import SafetyScreen

    class Provider:
        calls = 0

        def structured(self, instruction, prompt, schema):
            assert schema is SafetyScreen and len(prompt) <= 12256
            self.calls += 1
            return SafetyScreen(urgent="внезапные тяжёлые симптомы" in prompt)

    voice = update("")
    voice["message"]["voice"] = {"file_id": "synthetic"}
    save_update(db, voice, 42)
    db.commit()
    transcript = "x" * position + " внезапные тяжёлые симптомы " + "x" * (25000 - position)
    provider = Provider()
    response = process_message(
        db_engine, provider, Settings(telegram_user_id=42), 1, transcript=transcript
    )
    assert "112" in response and provider.calls <= 3
    db.expire_all()
    assert db.get(TelegramUpdate, 1).status == "processed"
    assert db.scalar(select(func.count()).select_from(Event)) == 0


@pytest.mark.parametrize("unavailable", [False, True])
def test_oversized_fallback_keeps_emergency_guidance(db, unavailable):
    from garmin_ai.agent import SafetyScreen
    from garmin_ai.llm import ProviderUnavailable

    class Provider:
        calls = 0

        def structured(self, *args):
            self.calls += 1
            if unavailable:
                raise ProviderUnavailable("synthetic")
            return SafetyScreen(urgent=False)

    provider = Provider()
    result = interpret(db, provider, "x" * 100000, Settings(), datetime.now(UTC))
    assert result.intent == "safety" and "112" in result.clarification
    assert provider.calls <= 4 and not result.events


@pytest.mark.parametrize("status", ["pending", "sent"])
def test_delete_migraine_retires_questions_immediately(db, status):
    from datetime import timedelta

    from garmin_ai.events import delete_event, undo_last
    from garmin_ai.models import PendingQuestion

    now = datetime.now(UTC)
    event = create_event(db, EventInput(start=now, payload={"type": "migraine"}), actor="owner")
    q = PendingQuestion(
        kind="migraine",
        event_id=event.id,
        text="synthetic",
        evidence={},
        priority=0.9,
        dedup_key="delete-migraine",
        earliest_send_at=now,
        expires_at=now + timedelta(days=2),
        status=status,
        sent_at=now if status == "sent" else None,
    )
    db.add(q)
    db.flush()
    delete_event(db, event.id, revision=1, actor="owner")
    assert q.status == "cancelled"
    undo_last(db, actor="owner")
    assert q.status == status


@pytest.mark.parametrize("status", ["inferred", "needs_confirmation"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_end_button_ignores_unconfirmed_episodes(db, status, confirmed):
    from datetime import timedelta

    from garmin_ai.telegram import handle_button

    now = datetime.now(UTC)
    draft = create_event(
        db,
        EventInput(start=now - timedelta(hours=3), status=status, payload={"type": "migraine"}),
        actor="owner",
    )
    real = (
        create_event(
            db,
            EventInput(start=now - timedelta(hours=2), payload={"type": "migraine"}),
            actor="owner",
        )
        if confirmed
        else None
    )
    response = handle_button(db, "end", Settings(), "owner", 101, now, time_known=True)
    assert draft.end is None and draft.status == status
    if real:
        assert real.end == now
    else:
        assert "Открытой мигрени нет" in response


@pytest.mark.parametrize("failure", ["transport", "server", "malformed"])
def test_oversized_provider_failure_sends_immediate_fallback(db, db_engine, failure):
    from types import SimpleNamespace

    from garmin_ai.llm import GeminiProvider

    def create(**kwargs):
        if failure == "malformed":
            return SimpleNamespace(output_text="not json")
        raise (ConnectionError if failure == "transport" else RuntimeError)("synthetic failure")

    provider = object.__new__(GeminiProvider)
    provider.model = "synthetic"
    provider.generation_config = {}
    provider.client = SimpleNamespace(interactions=SimpleNamespace(create=create))
    voice = update("")
    voice["message"]["voice"] = {"file_id": "synthetic"}
    save_update(db, voice, 42)
    db.commit()
    response = process_message(db_engine, provider, Settings(telegram_user_id=42), 1, "x" * 20000)
    assert "112" in response
    db.expire_all()
    assert db.get(TelegramUpdate, 1).status == "processed"
    assert db.scalar(select(func.count()).select_from(Event)) == 0


@pytest.mark.parametrize("status", ["inferred", "needs_confirmation"])
def test_free_text_cannot_close_unconfirmed_migraine(db, db_engine, status):
    from datetime import timedelta

    now = datetime.fromtimestamp(update()["message"]["date"], UTC)
    episode = create_event(
        db,
        EventInput(
            start=now - timedelta(hours=3),
            timezone="UTC",
            status=status,
            payload={"type": "migraine"},
        ),
        actor="owner",
    )
    identity = episode.id
    save_update(db, update("Закончилась сейчас"), 42)
    db.commit()
    provider = FakeProvider(
        Interpretation(
            intent="close",
            confidence=1,
            target_event_id=identity,
            events=[
                EventInput(
                    start=now - timedelta(hours=3),
                    end=now,
                    timezone="UTC",
                    payload={"type": "migraine"},
                )
            ],
        )
    )
    response = process_message(db_engine, provider, Settings(telegram_user_id=42), 1)
    assert "Ничего не изменено" in response
    db.expire_all()
    assert db.get(Event, identity).end is None and db.get(Event, identity).status == status


@pytest.mark.parametrize("legacy", [False, True])
def test_poll_reestablishes_offset_after_idle_week(db, db_engine, legacy):
    from datetime import timedelta
    from types import SimpleNamespace

    from garmin_ai.telegram import poll

    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    db.add(
        AppState(
            key="telegram:offset", value={"offset": 901, **({} if legacy else {"received_at": old})}
        )
    )
    db.add(AppState(key="telegram:ordering", value={"epoch": 0, "last_received_at": old}))
    db.commit()
    offsets = []

    async def run():
        stop = asyncio.Event()

        async def get_updates(**kwargs):
            offsets.append(kwargs["offset"])
            if len(offsets) == 1:
                return [
                    SimpleNamespace(update_id=10, to_dict=lambda: update("/status", update_id=10))
                ]
            stop.set()
            return []

        await poll(
            SimpleNamespace(get_updates=get_updates), db_engine, Settings(telegram_user_id=42), stop
        )

    asyncio.run(run())
    assert offsets == [None, 11]
    db.expire_all()
    assert db.get(TelegramUpdate, 10).payload["_ordering_epoch"] == 1


@pytest.mark.parametrize("stage", ["initialize", "get_webhook_info"])
def test_telegram_startup_outage_does_not_stop_garmin(db, db_engine, tmp_path, monkeypatch, stage):
    from garmin_ai import runtime
    from garmin_ai.jobs import enqueue
    from garmin_ai.models import Job

    save_update(db, update("/undo"), 42)
    enqueue(db, "garmin_endpoint", {}, "synthetic-garmin", datetime.now(UTC))
    db.commit()
    calls = []

    class OfflineBot:
        def __init__(self, *args):
            pass

        async def initialize(self):
            if stage == "initialize":
                raise ConnectionError("synthetic")

        async def get_webhook_info(self):
            raise ConnectionError("synthetic")

        async def shutdown(self):
            pass

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="synthetic",
        telegram_user_id=42,
        llm_enabled=False,
    )
    monkeypatch.setattr(runtime, "Bot", OfflineBot)
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)
    monkeypatch.setattr(runtime.GarminReader, "restore", lambda _: object())
    monkeypatch.setattr(runtime, "run_garmin_job", lambda *args: calls.append(True))

    async def run():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(), "add_signal_handler", lambda s, cb: callbacks.append(cb)
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if calls:
                    break
            assert calls and not task.done()
        finally:
            callbacks[0]()
            await asyncio.wait_for(task, 3)

    asyncio.run(run())
    db.expire_all()
    assert db.get(AppState, "runtime:heartbeat") is not None
    queued = db.scalar(select(Job).where(Job.dedup_key == "telegram:1"))
    assert queued.status == "pending" and queued.attempts == 0
    assert db.scalar(select(Job).where(Job.dedup_key == "synthetic-garmin")).status == "done"


def test_delivery_formats_and_resumes_without_duplicates(db, db_engine):
    from types import SimpleNamespace

    class Bot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(message_id=len(self.calls))

    bot = Bot()
    source = "**" + "😀" * 2000 + "**"
    asyncio.run(deliver(bot, db_engine, 42, "formatted", source))
    asyncio.run(deliver(bot, db_engine, 42, "formatted", source))
    assert len(bot.calls) == 2
    assert "".join(call["text"] for call in bot.calls) == "😀" * 2000
    assert all(call["entities"][0].type == "bold" for call in bot.calls)
    assert all(call["parse_mode"] is None for call in bot.calls)


def test_delivery_keeps_legacy_partial_boundaries(db, db_engine):
    from types import SimpleNamespace

    db.add(AppState(key="outbox:legacy:0", value={"status": "sent", "message_id": 1}))
    db.commit()
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=2)

    source = "**" + "x" * 3500 + "**"
    asyncio.run(deliver(Bot(), db_engine, 42, "legacy", source))
    assert len(calls) == 1
    assert calls[0]["text"] == source[3500:]
    assert calls[0]["entities"] == []
