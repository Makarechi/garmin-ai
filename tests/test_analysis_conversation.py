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
