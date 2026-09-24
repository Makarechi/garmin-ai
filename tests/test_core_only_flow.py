"""Database-backed acceptance flow that needs no optional provider SDK."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.models import Event
from garmin_ai.tracker_forms import (
    FormSubmission,
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    action_for_event,
    confirm_tracker,
    form_for_action,
    preview_tracker,
    submit_form,
)


def test_core_setup_write_edit_and_restart_read(db, db_engine):
    draft = TrackerSetupDraft(
        key="core_smoke",
        name="Core smoke",
        locale="en",
        fields=[
            TrackerFieldDraft(key="rating", label="Rating", kind="scale", minimum=1, maximum=5),
            TrackerFieldDraft(
                key="count",
                label="Count",
                kind="integer",
                minimum=0,
                maximum=100,
                unit="count",
                metric_semantics="event_count",
            ),
            TrackerFieldDraft(key="note", label="Note", kind="text"),
        ],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="core-smoke",
    )
    action_id = created["action"]["id"]
    form = form_for_action(db, action_id)
    start = datetime(2026, 9, 20, 12, tzinfo=UTC)
    event = submit_form(
        db,
        action_id,
        FormSubmission(
            action_id=action_id,
            schema_hash=form.schema_hash,
            submission_id=form.submission_id,
            start=start,
            timezone="UTC",
            values={"rating": 3, "count": 2, "note": "synthetic"},
            units={"count": "count"},
        ),
        actor="core-smoke",
    )
    edit = action_for_event(db, event.id)
    edit_form = form_for_action(db, edit.id)
    updated = submit_form(
        db,
        edit.id,
        FormSubmission(
            action_id=edit.id,
            schema_hash=edit_form.schema_hash,
            start=start,
            timezone="UTC",
            values={"rating": 4, "count": 2, "note": "synthetic"},
            units={"count": "count"},
        ),
        actor="core-smoke",
    )
    assert updated.id == event.id and updated.revision == 2
    event_id = event.id
    db.commit()

    with Session(db_engine) as restarted:
        persisted = restarted.scalar(select(Event).where(Event.id == event_id))
        assert persisted is not None and persisted.deleted is False
        assert persisted.payload["rating"] == 4
        assert persisted.payload["count"] == 2
        assert persisted.payload["note"] == "synthetic"
        assert action_for_event(restarted, event_id).revision == 2
