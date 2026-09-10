from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from garmin_ai.agent import Interpretation, apply_command
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import Activity, AppState, Event, PendingQuestion, TimelineInterval
from garmin_ai.proactive import add_question, context_coverage, context_explained, reconcile_answers
from garmin_ai.queries import timeline

START = datetime(2026, 9, 10, 12, tzinfo=UTC)
END = START + timedelta(minutes=40)


@pytest.mark.parametrize("duration", [None, 1])
def test_point_or_short_note_does_not_explain_long_interval(db, duration):
    create_event(
        db,
        EventInput(
            start=START,
            end=START + timedelta(minutes=duration) if duration else None,
            payload={"type": "note", "description": "synthetic"},
        ),
        actor="test",
    )
    coverage = context_coverage(db, START, END)
    assert coverage["uncovered_seconds"] == (40 - (duration or 0)) * 60
    assert not context_explained(db, START, END)
    add_question(
        db,
        "context",
        "synthetic",
        {"start": START.isoformat(), "end": END.isoformat()},
        0.8,
        "synthetic",
        END,
    )
    reconcile_answers(db, END)
    assert db.scalar(select(PendingQuestion)).status == "pending"


def test_adjacent_annotations_cover_without_double_counting(db):
    for start, end in [
        (START, START + timedelta(minutes=25)),
        (START + timedelta(minutes=20), END),
    ]:
        create_event(
            db,
            EventInput(
                start=start, end=end, payload={"type": "context", "description": "synthetic"}
            ),
            actor="test",
        )
    assert context_explained(db, START, END)
    assert context_coverage(db, START, END)["covered_seconds"] == 40 * 60


def test_sleep_activity_symptoms_and_calendar_remain_in_separate_layers(db):
    db.add(Activity(id="synthetic", start=START, end=END, kind="running", timezone="UTC"))
    for identity, label, source in [
        ("sleep", "sleep", "garmin_connect"),
        ("plan", "meeting", "calendar"),
    ]:
        db.add(
            TimelineInterval(
                id=identity,
                start=START,
                end=END,
                label=label,
                source=source,
                confidence=1,
                confirmed=True,
                evidence={"synthetic": identity},
            )
        )
    create_event(
        db, EventInput(start=START - timedelta(days=1), payload={"type": "migraine"}), actor="test"
    )
    create_event(
        db,
        EventInput(
            start=START + timedelta(minutes=1),
            payload={"type": "note", "description": "synthetic point"},
        ),
        actor="test",
    )
    db.flush()
    result = timeline(db, START, END)
    assert all(
        len(result["layers"][layer]) == 1
        for layer in ("sleep", "activity", "wellbeing", "plans", "context")
    )
    assert result["layers"]["plans"][0]["status"] == "planned"
    assert result["layers"]["wellbeing"][0]["topology"] == "open_interval"
    assert result["layers"]["context"][0]["start"] == result["layers"]["context"][0]["end"]
    assert len(result["segments"][0]["annotations"]) == 4


def test_calendar_plan_does_not_establish_actual_context(db):
    db.add(
        TimelineInterval(
            id="synthetic",
            start=START,
            end=END,
            label="meeting",
            source="calendar",
            confidence=1,
            confirmed=True,
            evidence={},
        )
    )
    db.flush()
    assert not context_explained(db, START, END)


def test_unknown_answer_stops_repeats_without_inventing_context(db):
    add_question(
        db,
        "context",
        "synthetic",
        {"start": START.isoformat(), "end": END.isoformat()},
        0.8,
        "synthetic",
        END,
    )
    question = db.scalar(select(PendingQuestion))
    question.status = "sent"
    db.add(AppState(key="conversation:pending", value={"text": "synthetic ambiguity"}))
    db.flush()
    reply = apply_command(
        db,
        Interpretation(intent="acknowledge", target_question_id=question.id, confidence=1),
        text="не помню",
        update_id=1,
        actor="test",
        now=END,
    )
    reconcile_answers(db, END)
    add_question(
        db, "context", "synthetic repeat", dict(question.evidence), 0.8, "another-key", END
    )
    assert db.scalars(select(PendingQuestion)).all() == [question]
    assert question.status == "acknowledged" and "неизвестным" in reply
    assert db.scalar(select(Event)) is None
    assert not context_explained(db, START, END)
    assert db.get(AppState, "conversation:pending") is None


def test_late_activity_retires_unknown_acknowledgement(db):
    add_question(
        db,
        "context",
        "synthetic",
        {"start": START.isoformat(), "end": END.isoformat(), "answer_kind": "unknown"},
        0.8,
        "late-context",
        END,
    )
    question = db.scalar(select(PendingQuestion))
    question.status = "acknowledged"
    db.add(Activity(id="late", kind="running", start=START, end=END, timezone="UTC"))
    db.flush()
    reconcile_answers(db, END)
    assert question.status == "cancelled"
    assert question.evidence["context_coverage"]["uncovered_seconds"] == 0
    db.delete(db.get(Activity, "late"))
    db.flush()
    reconcile_answers(db, END)
    assert question.status == "acknowledged"
    assert question.evidence["context_coverage"]["uncovered_seconds"] > 0


def test_late_partial_activity_refreshes_question_evidence_and_text(db, monkeypatch):
    from garmin_ai.config import Settings
    from garmin_ai.proactive import select_question

    add_question(
        db,
        "context",
        "synthetic",
        {
            "start": START.isoformat(),
            "end": END.isoformat(),
            "timezone": "UTC",
            "context_coverage": context_coverage(db, START, END),
        },
        0.8,
        "partial-context",
        END,
    )
    db.add(
        Activity(
            id="partial",
            kind="running",
            start=START,
            end=START + timedelta(minutes=10),
            timezone="UTC",
        )
    )
    db.flush()
    monkeypatch.setattr(
        "garmin_ai.proactive.context_physiology", lambda *args, **kwargs: {"synthetic": True}
    )
    selected = select_question(db, Settings(proactive_enabled=True, timezone="UTC"), END)
    assert selected is not None
    assert selected.evidence["context_coverage"]["uncovered_seconds"] == 1800
    assert "30 мин" in selected.text and "12:10" in selected.text
