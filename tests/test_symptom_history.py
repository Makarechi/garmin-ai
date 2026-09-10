from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from garmin_ai.agent import Interpretation, apply_command, interpret
from garmin_ai.analytics import headache_day_coverage
from garmin_ai.config import Settings
from garmin_ai.events import (
    Conflict,
    EventInput,
    create_event,
    delete_event,
    undo_last,
    update_event,
)
from garmin_ai.models import Event
from garmin_ai.queries import list_events

NOW = datetime(2026, 9, 10, 17, tzinfo=UTC)


@pytest.mark.parametrize("status", ["inferred", "needs_confirmation"])
def test_parent_status_change_and_undo_preserve_confirmed_relation(db, status):
    original = create_event(
        db, EventInput(start=NOW, status=status, payload={"type": "migraine"}), actor="owner"
    )
    confirmed = EventInput(start=NOW, payload={"type": "migraine"})
    update_event(db, original.id, confirmed, revision=original.revision, actor="owner")
    create_event(db, observation(original.id), actor="observer")
    with pytest.raises(Conflict, match="symptom observations"):
        update_event(
            db,
            original.id,
            confirmed.model_copy(update={"status": status}),
            revision=original.revision,
            actor="owner",
        )
    with pytest.raises(Conflict, match="symptom observations"):
        undo_last(db, actor="owner")
    assert original.status == "confirmed"


@pytest.mark.parametrize("matching", [True, False])
def test_button_refinement_accepts_only_linked_symptom_logs(db, matching):
    from garmin_ai.telegram import handle_button

    handle_button(db, "migraine", Settings(), "owner", 100, NOW - timedelta(hours=1))
    original = db.scalar(select(Event).where(Event.kind == "migraine"))
    other = episode(db)
    command = Interpretation(
        intent="log",
        confidence=1,
        events=[observation(original.id if matching else other.id)],
    )

    class Provider:
        def structured(self, instruction, prompt, schema):
            return command

    result = interpret(db, Provider(), "стало 3/10 в 17:00", Settings(), NOW)
    assert result.intent == ("log" if matching else "clarify")
    if matching:
        apply_command(db, result, text="стало 3/10 в 17:00", update_id=101, actor="owner", now=NOW)
        assert original.revision == 1
        child = db.scalar(select(Event).where(Event.kind == "symptom_observation"))
        assert child.payload["severity"] == 3
        assert child.payload["episode_id"] == str(original.id)


def episode(db):
    return create_event(
        db,
        EventInput(
            start=NOW - timedelta(hours=3),
            timezone="UTC",
            payload={"type": "migraine", "severity": 7},
        ),
        actor="owner",
    )


def observation(identity, severity=3):
    return EventInput(
        start=NOW,
        timezone="UTC",
        payload={"type": "symptom_observation", "episode_id": identity, "severity": severity},
    )


def test_later_pain_observation_preserves_initial_severity_and_retries(db):
    original = episode(db)
    command = Interpretation(intent="log", confidence=1, events=[observation(original.id)])
    for _ in range(2):
        apply_command(db, command, text="стало 3/10 в 17:00", update_id=99, actor="owner", now=NOW)
    rows = list_events(db, NOW - timedelta(hours=4), NOW + timedelta(hours=1))["rows"]
    assert len(rows) == 2
    assert rows[0]["payload"]["severity"] == 7
    assert rows[1]["payload"]["severity"] == 3
    assert rows[1]["payload"]["episode_id"] == str(original.id)
    assert rows[1]["topology"] == "point"
    assert original.revision == 1


def test_unknown_episode_is_rejected_without_creating_observation(db):
    with pytest.raises(ValueError, match="existing confirmed migraine"):
        create_event(db, observation(uuid4()), actor="owner")
    assert db.scalar(select(Event)) is None


def test_episode_cannot_be_deleted_while_observation_is_linked(db):
    original = episode(db)
    child = create_event(db, observation(original.id), actor="owner")
    with pytest.raises(Conflict, match="symptom observations"):
        delete_event(db, original.id, revision=original.revision, actor="owner")
    delete_event(db, child.id, revision=child.revision, actor="owner")
    delete_event(db, original.id, revision=original.revision, actor="owner")
    assert original.deleted


@pytest.mark.parametrize("severity, expected", [(3, "positive"), (0, "confirmed_negative")])
def test_point_symptom_vetoes_full_day_negative_control(db, severity, expected):
    original = episode(db)
    symptom = create_event(db, observation(original.id, severity), actor="owner")
    left, right = NOW.replace(hour=0), NOW.replace(hour=0) + timedelta(days=1)
    negative = create_event(
        db,
        EventInput(
            start=left,
            end=right,
            timezone="UTC",
            payload={"type": "headache_observation", "headache": "no", "migraine": "no"},
        ),
        actor="owner",
    )
    assert headache_day_coverage([negative, symptom], left, right) == expected


def test_acknowledgement_observation_does_not_overwrite_episode(db):
    from garmin_ai.models import PendingQuestion

    original = episode(db)
    question = PendingQuestion(
        kind="migraine",
        event_id=original.id,
        text="synthetic",
        evidence={},
        priority=1,
        earliest_send_at=NOW - timedelta(minutes=20),
        sent_at=NOW - timedelta(minutes=10),
        expires_at=NOW + timedelta(days=1),
        status="sent",
        dedup_key="synthetic",
    )
    db.add(question)
    db.flush()
    command = Interpretation(
        intent="acknowledge",
        confidence=1,
        target_question_id=question.id,
        events=[observation(original.id)],
        changed_fields=["payload.severity"],
    )

    class Provider:
        def structured(self, instruction, prompt, schema):
            return command

    result = interpret(db, Provider(), "стало 3/10 в 17:00", Settings(), NOW)
    apply_command(db, result, text="стало 3/10 в 17:00", update_id=123, actor="owner", now=NOW)
    assert original.revision == 1
    assert original.payload["severity"] == 7
    child = db.scalar(select(Event).where(Event.kind == "symptom_observation"))
    assert child.payload["severity"] == 3
    assert child.payload["episode_id"] == str(original.id)
    assert question.status == "acknowledged"
