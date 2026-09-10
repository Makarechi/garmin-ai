import json
from datetime import UTC, datetime, timedelta

from garmin_ai import agent
from garmin_ai.config import Settings
from garmin_ai.conversation import (
    KEY,
    MAX_BYTES,
    PENDING_KEY,
    conversation_context,
    forget_conversation,
    remember_answer,
)
from garmin_ai.models import AppState

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


class Provider:
    def __init__(self):
        self.prompts = []

    def structured(self, instruction, prompt, schema):
        if schema is agent.SafetyScreen:
            return agent.SafetyScreen(urgent=False)
        self.prompts.append(json.loads(prompt))
        if len(self.prompts) == 1:
            return agent.AgentStep(
                calls=[
                    agent.ReadCall(
                        name="personal_baseline",
                        arguments_json='{"metric":"sleep_score","start":"2026-09-01","end":"2026-09-09"}',
                    )
                ]
            )
        return agent.AgentStep(answer="Synthetic answer", evidence_ids=[1])


def remember(
    db, identity, now=NOW, question="Synthetic question", answer="Synthetic answer", epoch=None
):
    db.merge(
        AppState(
            key=f"outbox:update:{identity}:0", value={"status": "sent", "message_id": identity}
        )
    )
    db.flush()
    remember_answer(
        db,
        now,
        identity,
        question,
        answer,
        [
            {
                "tool": "personal_baseline",
                "result": {"mean": 78},
                "arguments": {"metric": "sleep_score"},
            }
        ],
        epoch=epoch,
    )


def test_analytical_followup_survives_restart_and_requeries_evidence(db, monkeypatch):
    calls = []
    monkeypatch.setattr(agent, "call_tool", lambda *args: calls.append(args) or {"mean": 78})
    first = Provider()
    agent.answer_question(db, first, "Как спал?", Settings(), NOW, update_id=1)
    db.add(AppState(key="outbox:update:1:0", value={"status": "sent", "message_id": 1}))
    db.commit()
    db.expire_all()
    second = Provider()
    agent.answer_question(
        db, second, "А за прошлую неделю?", Settings(), NOW + timedelta(minutes=1), update_id=2
    )
    previous = second.prompts[0]["conversation"]["turns"][0]
    assert previous["question"] == "Как спал?"
    assert previous["asked_at"] == NOW.isoformat()
    assert previous["specs"][0]["arguments"]["metric"] == "sleep_score"
    assert len(calls) == 2
    assert not second.prompts[0]["evidence"]


def test_reply_to_selects_original_question_instead_of_latest(db):
    remember(db, 10, question="Original sleep question")
    remember(db, 20, now=NOW + timedelta(minutes=1), question="New running topic")
    db.merge(AppState(key="outbox:update:10:0", value={"status": "sent", "message_id": 501}))
    db.flush()
    context = conversation_context(db, NOW + timedelta(minutes=2), 501)
    assert context["explicit_reply"] and not context["selection_missing"]
    assert [turn["update_id"] for turn in context["turns"]] == ["10"]


def test_unknown_reply_does_not_fall_back_to_recent_analysis(db):
    remember(db, 1)
    provider = Provider()
    response = agent.answer_question(
        db, provider, "Почему?", Settings(), NOW, reply_to_message_id=999
    )
    assert "недоступен" in response
    assert not provider.prompts


def test_forget_fences_inflight_answer_and_removes_future_context(db):
    remember(db, 1)
    epoch = conversation_context(db, NOW)["epoch"]
    forget_conversation(db)
    remember(db, 2, epoch=epoch)
    assert not conversation_context(db, NOW)["turns"]
    next_epoch = conversation_context(db, NOW)["epoch"]
    remember(db, 3, epoch=next_epoch)
    assert [turn["update_id"] for turn in conversation_context(db, NOW)["turns"]] == ["3"]


def test_context_has_count_age_and_utf8_limits(db):
    for identity in range(20):
        remember(
            db,
            identity,
            now=NOW + timedelta(minutes=identity),
            question="я" * 1000,
            answer="я" * 1500,
        )
    value = db.get(AppState, KEY, populate_existing=True).value
    assert 0 < len(value["turns"]) <= 6
    assert len(json.dumps(value["turns"], ensure_ascii=False).encode("utf-8")) <= MAX_BYTES
    assert not conversation_context(db, NOW + timedelta(days=8))["turns"]


def test_analysis_topic_is_not_a_diary_mutation_target(db):
    remember(db, 1, question="Synthetic medication question")
    context = agent.context_for(db, NOW)
    assert context["recent_analysis_question"]["question"] == "Synthetic medication question"
    assert context["recent_events"] == []
    assert context["pending_clarification"] is None


def test_expired_context_is_physically_pruned_without_new_answer(db):
    from garmin_ai.conversation import prune_conversation

    remember(db, 1)
    prune_conversation(db, NOW + timedelta(days=8))
    db.commit()
    assert db.get(AppState, KEY, populate_existing=True).value["turns"] == []


def test_escaped_control_characters_cannot_exceed_storage_cap(db):
    remember(db, 1, question="\x00" * 1000, answer="\x01" * 1500)
    row = db.get(AppState, KEY, populate_existing=True)
    assert (
        row is None or len(json.dumps(row.value, ensure_ascii=False).encode("utf-8")) <= MAX_BYTES
    )


def test_dropped_arguments_are_explicitly_marked(db):
    remember_answer(
        db,
        NOW,
        1,
        "Question",
        "Answer",
        [{"tool": "synthetic", "arguments": {"filter": "x" * 1100}, "result": {"mean": 78}}],
        epoch=None,
    )
    turn = db.get(AppState, PENDING_KEY, populate_existing=True).value["turn"]
    assert turn["specs_truncated"] and turn["specs"][0]["arguments"] is None


def test_uncertain_delivery_is_excluded_until_confirmed_sent(db):
    remember(db, 1)
    delivery = db.get(AppState, "outbox:update:1:0")
    delivery.value = {"status": "uncertain", "message_id": 1}
    db.flush()
    assert not conversation_context(db, NOW)["turns"]
    assert conversation_context(db, NOW, 1)["selection_missing"]
    delivery.value = {"status": "sent", "message_id": 1}
    db.flush()
    assert len(conversation_context(db, NOW)["turns"]) == 1


def test_forget_command_bypasses_delayed_diary_and_fences_old_epoch(db, db_engine):
    from sqlalchemy import select

    from garmin_ai.jobs import claim
    from garmin_ai.models import Job
    from garmin_ai.telegram import process_message, save_update

    remember(db, 1)
    for identity, text in [(10, "synthetic question"), (11, "/forget_conversation")]:
        save_update(
            db,
            {
                "update_id": identity,
                "message": {
                    "message_id": identity,
                    "date": int(NOW.timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": text,
                },
            },
            42,
        )
    pending = db.scalar(select(Job).where(Job.kind == "telegram_update"))
    pending.run_at = NOW + timedelta(days=1)
    db.commit()
    claimed = claim(db, kinds=["telegram_update", "telegram_control"], now=NOW)
    assert claimed.kind == "telegram_control"
    db.commit()
    response = process_message(db_engine, None, Settings(telegram_user_id=42), 11)
    assert "очищен" in response
    remember_answer(db, NOW, 12, "Question", "Answer", [], epoch=None)
    assert not conversation_context(db, NOW)["turns"]


def test_failed_deliveries_do_not_evict_confirmed_conversation(db):
    for identity in range(6):
        remember(db, identity)
    for identity in range(10, 30):
        remember_answer(db, NOW, identity, "Undelivered", "Answer", [], epoch=None)
    assert [turn["update_id"] for turn in conversation_context(db, NOW)["turns"]] == [
        str(i) for i in range(6)
    ]
    pending = db.get(AppState, PENDING_KEY, populate_existing=True).value
    assert pending["turn"]["update_id"] == "29"
    db.add(AppState(key="outbox:update:29:0", value={"status": "sent", "message_id": 29}))
    db.flush()
    assert [turn["update_id"] for turn in conversation_context(db, NOW)["turns"]] == [
        str(i) for i in range(1, 6)
    ] + ["29"]
    assert db.get(AppState, PENDING_KEY, populate_existing=True) is None


def test_forget_removes_pending_turn_before_delivery(db):
    remember_answer(db, NOW, 1, "Undelivered", "Answer", [], epoch=None)
    forget_conversation(db)
    db.add(AppState(key="outbox:update:1:0", value={"status": "sent", "message_id": 1}))
    db.flush()
    assert not conversation_context(db, NOW)["turns"]
    assert db.get(AppState, PENDING_KEY, populate_existing=True) is None


def test_older_message_clock_hides_but_does_not_delete_newer_turn(db):
    remember(db, 1, now=NOW)
    assert not conversation_context(db, NOW - timedelta(minutes=1))["turns"]
    assert conversation_context(db, NOW)["turns"][0]["update_id"] == "1"


def test_forget_during_model_call_stops_followup_and_answer(db, monkeypatch):
    remember(db, 1)
    db.commit()
    calls = []
    monkeypatch.setattr(agent, "call_tool", lambda *args: calls.append(args) or {"mean": 78})

    class ForgettingProvider(Provider):
        def structured(self, instruction, prompt, schema):
            step = super().structured(instruction, prompt, schema)
            forget_conversation(db)
            db.flush()
            return step

    provider = ForgettingProvider()
    result = agent.answer_question(db, provider, "followup", Settings(), NOW, update_id=2)
    assert "Контекст разговора удалён" in result
    assert len(provider.prompts) == 1 and not calls
    assert db.get(AppState, PENDING_KEY) is None


def test_reply_fragment_routes_to_selected_analysis_before_interpretation(
    db, db_engine, monkeypatch
):
    from garmin_ai import telegram

    now = datetime.now(UTC)
    remember(db, 10, now=now, question="Original sleep question")
    remember(db, 20, now=now, question="Unrelated running question")
    telegram.save_update(
        db,
        {
            "update_id": 30,
            "message": {
                "message_id": 30,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "date": now.isoformat(),
                "text": "только будни",
                "reply_to_message": {"message_id": 10},
            },
        },
        42,
    )
    db.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("Explicit reply must not use unselected intent context")

    monkeypatch.setattr(telegram, "interpret", forbidden)
    monkeypatch.setattr(agent, "call_tool", lambda *args: {"mean": 78})
    provider = Provider()
    telegram.process_message(db_engine, provider, Settings(telegram_user_id=42), 30)
    assert provider.prompts[0]["conversation"]["turns"][0]["question"] == "Original sleep question"


def test_nonanalytic_reply_preserves_diary_interpretation(db, db_engine, monkeypatch):
    from garmin_ai import telegram

    now = datetime.now(UTC)
    telegram.save_update(
        db,
        {
            "update_id": 30,
            "message": {
                "message_id": 30,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "date": now.isoformat(),
                "text": "уточнение",
                "reply_to_message": {"message_id": 999},
            },
        },
        42,
    )
    db.commit()
    calls = []
    monkeypatch.setattr(
        telegram,
        "interpret",
        lambda *args, **kwargs: (
            calls.append(True)
            or agent.Interpretation(
                intent="clarify", confidence=0, clarification="Diary clarification"
            )
        ),
    )
    assert (
        telegram.process_message(db_engine, Provider(), Settings(telegram_user_id=42), 30)
        == "Diary clarification"
    )
    assert calls


def test_urgent_queued_reply_bypasses_delayed_diary(db, db_engine):
    from garmin_ai import telegram

    now = datetime.now(UTC)
    for identity in (10, 11):
        telegram.save_update(
            db,
            {
                "update_id": identity,
                "message": {
                    "message_id": identity,
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "date": now.isoformat(),
                    "text": "urgent synthetic" if identity == 11 else "earlier",
                    **({"reply_to_message": {"message_id": 999}} if identity == 11 else {}),
                },
            },
            42,
        )
    db.commit()

    class UrgentProvider:
        def structured(self, instruction, text, schema):
            assert schema is agent.SafetyScreen
            return agent.SafetyScreen(urgent=True)

    assert "112" in telegram.process_message(
        db_engine, UrgentProvider(), Settings(telegram_user_id=42), 11
    )


def test_analytic_outbox_kind_survives_context_expiry(db):
    from garmin_ai.conversation import is_analytic_reply, prune_conversation

    remember(db, 1)
    db.get(AppState, "outbox:update:1:0").value = {
        "status": "sent",
        "message_id": 1,
        "kind": "analysis",
    }
    db.flush()
    prune_conversation(db, NOW + timedelta(days=8))
    assert is_analytic_reply(db, 1)
    assert conversation_context(db, NOW + timedelta(days=8), 1)["selection_missing"]


def test_runtime_forget_control_runs_while_analysis_worker_is_blocked(
    db, db_engine, tmp_path, monkeypatch
):
    import asyncio
    import threading
    from types import SimpleNamespace

    from garmin_ai import runtime
    from garmin_ai.db import transaction
    from garmin_ai.telegram import save_update

    def update(identity, text):
        return {
            "update_id": identity,
            "message": {
                "message_id": identity,
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": text,
            },
        }

    remember(db, 1, now=datetime.now(UTC))
    save_update(db, update(10, "Synthetic analysis"), 42)
    db.commit()
    entered, release = threading.Event(), threading.Event()
    original = runtime.process_message

    def blocked_analysis(engine, provider, settings, identity, *args, **kwargs):
        if identity == 10:
            entered.set()
            assert release.wait(8)
            return "Synthetic completed answer"
        return original(engine, provider, settings, identity, *args, **kwargs)

    class Bot:
        def __init__(self, *args):
            pass

        async def initialize(self):
            pass

        async def get_webhook_info(self):
            return SimpleNamespace(url="https://synthetic.invalid", pending_update_count=0)

        async def set_webhook(self, **kwargs):
            pass

        async def shutdown(self):
            pass

        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=100)

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="synthetic",
        telegram_user_id=42,
        telegram_webhook_secret="synthetic-secret-for-testing",
        llm_enabled=False,
    )
    monkeypatch.setattr(runtime, "Bot", Bot)
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)
    monkeypatch.setattr(runtime, "process_message", blocked_analysis)

    async def run():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "add_signal_handler",
            lambda signal, cb: callbacks.append(cb),
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.02)
            assert entered.is_set()
            with transaction(db_engine) as session:
                save_update(session, update(11, "/forget_conversation"), 42)
            handled = False
            for _ in range(150):
                await asyncio.sleep(0.02)
                with transaction(db_engine) as session:
                    handled = session.get(AppState, "telegram:reply:11") is not None
                    if handled:
                        assert not conversation_context(session, datetime.now(UTC))["turns"]
                        break
            assert handled and not release.is_set()
        finally:
            release.set()
            callbacks[0]()
            await asyncio.wait_for(task, 5)

    asyncio.run(run())


def test_delivery_preserves_analytic_reply_marker(db, db_engine):
    import asyncio
    from types import SimpleNamespace

    from garmin_ai.conversation import is_analytic_reply
    from garmin_ai.telegram import deliver

    db.add(
        AppState(
            key="telegram:reply:70",
            value={"kind": "analysis", "text": "Synthetic", "status": "pending"},
        )
    )
    db.commit()

    async def send_message(**kwargs):
        return SimpleNamespace(message_id=701)

    asyncio.run(
        deliver(SimpleNamespace(send_message=send_message), db_engine, 42, "update:70", "Synthetic")
    )
    db.expire_all()
    assert is_analytic_reply(db, 701)


def test_forget_fences_cached_analysis_delivery_retry(db, db_engine):
    import asyncio
    from types import SimpleNamespace

    from telegram.error import RetryAfter

    from garmin_ai.db import transaction
    from garmin_ai.telegram import deliver

    db.add(
        AppState(
            key="telegram:reply:70",
            value={
                "text": "Synthetic private analysis",
                "kind": "analysis",
                "analysis_epoch": None,
                "status": "pending",
            },
        )
    )
    db.commit()
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        raise RetryAfter(1)

    bot = SimpleNamespace(send_message=send_message)

    async def run():
        import pytest

        with pytest.raises(RetryAfter):
            await deliver(bot, db_engine, 42, "update:70", "Synthetic private analysis")
        with transaction(db_engine) as session:
            forget_conversation(session)
        await deliver(bot, db_engine, 42, "update:70", "Synthetic private analysis")

    asyncio.run(run())
    assert len(calls) == 1
    db.expire_all()
    assert "Synthetic private analysis" not in str(db.get(AppState, "telegram:reply:70").value)


def test_forget_between_analytical_parts_stops_remaining_delivery(db, db_engine):
    import asyncio
    from types import SimpleNamespace

    from garmin_ai.db import transaction
    from garmin_ai.telegram import deliver

    db.add(
        AppState(
            key="telegram:reply:70",
            value={
                "kind": "analysis",
                "analysis_epoch": None,
                "text": "Synthetic",
                "status": "pending",
            },
        )
    )
    db.commit()
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        with transaction(db_engine) as session:
            forget_conversation(session)
        return SimpleNamespace(message_id=701)

    asyncio.run(
        deliver(SimpleNamespace(send_message=send_message), db_engine, 42, "update:70", "x" * 7000)
    )
    assert len(calls) == 1


def test_known_analytical_reply_is_not_consumed_by_an_active_note_form(db, db_engine):
    from garmin_ai.telegram import process_message, save_update

    now = datetime.now(UTC)
    remember(db, 1, now=now - timedelta(seconds=2))
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "button": "note",
                "action": "log",
                "created_at": now.isoformat(),
                "event_ids": [],
            },
        )
    )
    save_update(
        db,
        {
            "update_id": 70,
            "message": {
                "message_id": 70,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "date": int(now.timestamp()),
                "text": "А за другой период?",
                "reply_to_message": {"message_id": 1},
            },
        },
        42,
    )
    db.commit()
    assert "Synthetic answer" in process_message(
        db_engine, Provider(), Settings(telegram_user_id=42), 70
    )
