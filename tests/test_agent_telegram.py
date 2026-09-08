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
