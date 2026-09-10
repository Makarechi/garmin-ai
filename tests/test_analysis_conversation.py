import json
from datetime import UTC, datetime, timedelta

from garmin_ai import agent
from garmin_ai.config import Settings
from garmin_ai.conversation import (
    KEY,
    MAX_BYTES,
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
    db.add(AppState(key="outbox:update:10:0", value={"status": "sent", "message_id": 501}))
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
