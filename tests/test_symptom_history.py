from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from garmin_ai.agent import Interpretation, apply_command
from garmin_ai.analytics import headache_day_coverage
from garmin_ai.events import Conflict, EventInput, create_event, delete_event
from garmin_ai.models import Event
from garmin_ai.queries import list_events

NOW = datetime(2026, 9, 10, 17, tzinfo=UTC)


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


def test_point_symptom_vetoes_full_day_negative_control(db):
    original = episode(db)
    symptom = create_event(db, observation(original.id), actor="owner")
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
    assert headache_day_coverage([negative, symptom], left, right) == "positive"
