from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from garmin_ai.events import ActivityEffort, EventInput, create_event, undo_last, update_event
from garmin_ai.models import Activity
from garmin_ai.queries import timeline
from garmin_ai.telegram import diary_label

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def activity(db):
    db.add(
        Activity(
            id="synthetic", start=NOW, end=NOW + timedelta(hours=1), kind="running", timezone="UTC"
        )
    )
    db.flush()


def report(**changes):
    return EventInput(
        start=NOW + timedelta(hours=1),
        timezone="UTC",
        payload={"type": "activity_effort", "activity_id": "synthetic", "perceived_exertion": 7},
        **changes,
    )


def test_effort_report_is_distinct_audited_and_visible(db):
    activity(db)
    value = report(end=NOW + timedelta(hours=1))
    row = create_event(db, value, actor="synthetic", idempotency_key="synthetic-effort")
    assert row.end is None
    assert (
        create_event(db, value, actor="synthetic", idempotency_key="synthetic-effort").id == row.id
    )
    assert "7/10" in diary_label(row)
    result = timeline(db, NOW, NOW + timedelta(hours=2))
    assert any(
        item["evidence"].get("event_id") == str(row.id) for item in result["layers"]["wellbeing"]
    )
    changed = value.model_copy(
        update={"payload": ActivityEffort(activity_id="synthetic", perceived_exertion=3)}
    )
    update_event(db, row.id, changed, revision=row.revision, actor="synthetic")
    assert row.payload["perceived_exertion"] == 3
    undo_last(db, actor="synthetic")
    db.refresh(row)
    assert row.payload["perceived_exertion"] == 7
    assert db.get(Activity, "synthetic").start == NOW


def test_effort_requires_existing_activity_and_valid_report_time(db):
    with pytest.raises(ValueError, match="existing activity"):
        create_event(db, report(), actor="synthetic")
    activity(db)
    with pytest.raises(ValueError, match="precede"):
        create_event(
            db, report().model_copy(update={"start": NOW - timedelta(seconds=1)}), actor="synthetic"
        )


@pytest.mark.parametrize("rating", [-1, 11, 1.5, True, "7"])
def test_effort_requires_explicit_integer_scale(rating):
    with pytest.raises(ValidationError):
        ActivityEffort(activity_id="synthetic", perceived_exertion=rating)


@pytest.mark.parametrize(
    "changes", [{"source": "inferred", "status": "inferred"}, {"end": NOW + timedelta(hours=2)}]
)
def test_effort_cannot_be_inferred_or_cover_a_duration(changes):
    with pytest.raises(ValidationError):
        report(**changes)
