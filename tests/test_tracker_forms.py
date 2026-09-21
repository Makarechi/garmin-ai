from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import (
    activate_definition,
    propose_definition_revision,
    retire_definition,
)
from garmin_ai.events import Conflict
from garmin_ai.models import Event, EventDefinition, PendingQuestion, TrackerConfig
from garmin_ai.proactive import generate_questions, select_question
from garmin_ai.queries import list_events
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    action_for_event,
    available_actions,
    confirm_tracker,
    definition_spec,
    form_for_action,
    preview_tracker,
    submit_form,
)

NOW = datetime(2026, 9, 20, 18, tzinfo=UTC)


def focus_draft(**changes):
    values = {
        "key": "focus_session",
        "name": "Focus session",
        "locale": "en",
        "topology": "bounded_interval",
        "fields": [
            TrackerFieldDraft(
                key="focus",
                label="Focus",
                kind="scale",
                minimum=1,
                maximum=5,
            ),
            TrackerFieldDraft(
                key="note",
                label="Note",
                kind="text",
                required=False,
                max_length=200,
            ),
        ],
        "shortcut": "Log focus",
        "reminder_enabled": True,
        "reminder_time": "20:30",
        "reminder_timezone": "Europe/Bratislava",
    }
    values.update(changes)
    return TrackerSetupDraft(**values)


def install(db, draft=None):
    draft = draft or focus_draft()
    preview = preview_tracker(db, draft)
    return confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )


def submission(form, **changes):
    values = {
        "action_id": form.id,
        "schema_hash": form.schema_hash,
        "submission_id": form.submission_id,
        "start": NOW,
        "end": NOW + timedelta(minutes=25),
        "timezone": "UTC",
        "values": {"focus": 4},
        "units": {"focus": "score_1-5"},
    }
    values.update(changes)
    return values


def test_preview_confirm_generated_form_create_edit_history_and_settings(db):
    draft = focus_draft()
    preview = preview_tracker(db, draft)
    assert preview["definition"]["key"] == "user.focus_session"
    assert [field["name"] for field in preview["form"]["fields"]] == ["focus", "note"]
    assert preview["form"]["fields"][1]["required"] is False

    created = install(db, draft)
    actions = available_actions(db)
    assert len(actions) == 1
    assert actions[0].label == "Log focus"
    assert created["action"]["id"] == actions[0].id
    form = form_for_action(db, actions[0].id)
    assert {field.name for field in form.fields} == {"focus", "note"}

    event = submit_form(db, form.id, submission(form), actor="test")
    assert event.payload == {"type": "user.focus_session", "focus": 4}
    edit = action_for_event(db, event.id)
    edit_form = form_for_action(db, edit.id)
    updated = submit_form(
        db,
        edit.id,
        submission(
            edit_form,
            action_id=edit.id,
            values={"focus": 5, "note": "synthetic"},
        ),
        actor="test",
    )

    assert updated.id == event.id and updated.revision == 2
    assert updated.payload["focus"] == 5
    rows = list_events(db, NOW - timedelta(minutes=1), NOW + timedelta(hours=1))["rows"]
    assert [row["id"] for row in rows] == [str(event.id)]
    tracker = db.scalar(select(TrackerConfig))
    assert tracker.reminder_enabled and tracker.reminder_time == "20:30"


def test_generated_create_form_replays_same_submission(db):
    install(db)
    form = form_for_action(db, available_actions(db)[0].id)
    assert form.submission_id
    first = submit_form(db, form.id, submission(form), actor="test")
    second = submit_form(db, form.id, submission(form), actor="test")
    assert second.id == first.id
    assert list(db.scalars(select(Event))) == [first]


def test_confirmed_tracker_reminder_is_scheduled_once_per_local_day(db):
    from garmin_ai.proactive import generate_questions, select_question

    install(db)
    due = NOW + timedelta(minutes=31)
    generate_questions(db, Settings(timezone="UTC"), due)
    generate_questions(db, Settings(timezone="UTC"), due + timedelta(minutes=31))
    reminders = list(
        db.scalars(select(PendingQuestion).where(PendingQuestion.kind == "tracker_reminder"))
    )
    assert len(reminders) == 1
    assert "Log focus" in reminders[0].text
    assert reminders[0].earliest_send_at <= due < reminders[0].expires_at
    assert select_question(db, Settings(timezone="UTC"), due, tracker_only=True) == reminders[0]


def test_confirmation_requires_live_server_preview_and_is_single_use(db):
    draft = focus_draft()

    with pytest.raises(Conflict, match="preview it again"):
        confirm_tracker(
            db,
            TrackerConfirmation(draft=draft, confirmation_token="0" * 64),
            actor="test",
        )

    preview = preview_tracker(db, draft)
    confirmation = TrackerConfirmation(
        draft=draft,
        confirmation_token=preview["confirmation_token"],
    )
    confirm_tracker(db, confirmation, actor="test")

    with pytest.raises(Conflict, match="preview it again"):
        confirm_tracker(db, confirmation, actor="test")


def test_retiring_tracker_cancels_queued_reminder(db):
    install(db, focus_draft(reminder_timezone="UTC"))
    now = NOW.replace(hour=21)
    generate_questions(db, Settings(timezone="UTC"), now)
    reminder = db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "tracker_reminder"))
    definition = db.scalar(
        select(EventDefinition).where(EventDefinition.key == "user.focus_session")
    )

    retire_definition(db, definition.id, definition.revision, authorized=True)
    db.refresh(reminder)
    assert reminder.status == "cancelled"
    selected = select_question(db, Settings(timezone="UTC", proactive_enabled=True), now)
    assert selected is None or selected.kind != "tracker_reminder"


def test_tracker_reminder_cooldown_is_per_tracker(db):
    install(db, focus_draft(reminder_timezone="UTC"))
    install(db, focus_draft(key="second_focus", name="Second focus", reminder_timezone="UTC"))
    now = NOW.replace(hour=21)
    settings = Settings(timezone="UTC", proactive_enabled=True, question_budget=2)
    generate_questions(db, settings, now)

    first = select_question(db, settings, now)
    second = select_question(db, settings, now)
    assert first is not None and second is not None
    assert first.evidence["tracker_id"] != second.evidence["tracker_id"]


def test_old_create_form_fails_after_definition_version_changes_but_old_entry_edits(db):
    install(db)
    action = available_actions(db)[0]
    old_form = form_for_action(db, action.id)
    event = submit_form(db, action.id, submission(old_form), actor="test")
    edit = action_for_event(db, event.id)
    edit_form = form_for_action(db, edit.id)
    definition = db.scalar(
        select(EventDefinition).where(EventDefinition.key == "user.focus_session")
    )
    revised = focus_draft(
        fields=[
            *focus_draft().fields,
            TrackerFieldDraft(
                key="interruptions", label="Interruptions", kind="integer", minimum=0, maximum=100
            ),
        ]
    )
    proposed = propose_definition_revision(
        db,
        definition.id,
        definition.revision,
        definition_spec(revised),
        actor="test",
        authorized=True,
    )
    activate_definition(db, definition.id, proposed.revision, actor="test", authorized=True)

    try:
        submit_form(db, action.id, submission(old_form), actor="test")
        raise AssertionError("stale create form was accepted")
    except Conflict:
        pass
    corrected = submit_form(
        db,
        edit.id,
        submission(edit_form, action_id=edit.id, values={"focus": 3}),
        actor="test",
    )
    assert corrected.payload["focus"] == 3


def test_edit_action_requires_definition_query_permission(db):
    install(db)
    definition = db.scalar(
        select(EventDefinition).where(EventDefinition.key == "user.focus_session")
    )
    restricted = definition_spec(focus_draft()).model_copy(
        update={"allowed_operations": {"create", "update"}}
    )
    proposed = propose_definition_revision(
        db,
        definition.id,
        definition.revision,
        restricted,
        actor="test",
        authorized=True,
    )
    activate_definition(db, definition.id, proposed.revision, actor="test", authorized=True)
    create_action = available_actions(db)[0]
    form = form_for_action(db, create_action.id)
    event = submit_form(db, form.id, submission(form), actor="test")

    with pytest.raises(LookupError, match="Editable tracker"):
        action_for_event(db, event.id)
    with pytest.raises(LookupError, match="Editable tracker"):
        form_for_action(db, f"edit:{event.id}:{event.revision}")


def test_manual_form_correction_clears_stale_extraction_evidence(db):
    install(db)
    create_form = form_for_action(db, available_actions(db)[0].id)
    event = submit_form(db, create_form.id, submission(create_form), actor="test")
    event.evidence_refs = [{"field_id": "user.focus_session.focus", "start": 0, "end": 1}]
    db.flush()
    edit = action_for_event(db, event.id)
    edit_form = form_for_action(db, edit.id)

    updated = submit_form(
        db,
        edit.id,
        submission(edit_form, action_id=edit.id, values={"focus": 5}),
        actor="test",
    )

    assert updated.evidence_refs == []


def test_api_tracker_flow_returns_safe_validation_and_exports_entry(db, db_engine):
    key = "tracker-api-key-" + "x" * 32
    client = TestClient(
        create_app(
            Settings(
                api_tokens=[
                    ApiToken(
                        key=key,
                        scopes={"manage:definitions", "read:diary", "write:diary"},
                    )
                ]
            ),
            db_engine,
        )
    )
    headers = {"Authorization": "Bearer " + key}
    draft = focus_draft().model_dump(mode="json")
    preview = client.post("/tracker-setups/preview", json=draft, headers=headers)
    assert preview.status_code == 200
    confirmed = client.post(
        "/tracker-setups",
        json={"draft": draft, "confirmation_token": preview.json()["confirmation_token"]},
        headers=headers,
    )
    assert confirmed.status_code == 200
    action = client.get("/actions", headers=headers).json()["actions"][0]
    form = client.get(f"/forms/{action['id']}", headers=headers).json()

    invalid = client.post(
        f"/forms/{action['id']}/submit",
        json={
            "action_id": action["id"],
            "schema_hash": form["schema_hash"],
            "start": NOW.isoformat(),
            "end": (NOW + timedelta(minutes=25)).isoformat(),
            "timezone": "UTC",
            "values": {"note": "private value is never echoed"},
            "units": {},
        },
        headers=headers,
    )
    assert invalid.status_code == 422
    assert invalid.json() == {
        "detail": "Form validation failed",
        "errors": [{"field": "focus", "code": "required", "message": "This field is required"}],
    }
    response = client.post(
        f"/forms/{action['id']}/submit",
        json={
            "action_id": action["id"],
            "schema_hash": form["schema_hash"],
            "start": NOW.isoformat(),
            "end": (NOW + timedelta(minutes=25)).isoformat(),
            "timezone": "UTC",
            "values": {"focus": 4},
            "units": {"focus": "score_1-5"},
        },
        headers=headers,
    )
    assert response.status_code == 200
    event_id = response.json()["id"]
    edit = client.get(f"/actions/events/{event_id}", headers=headers)
    assert edit.status_code == 200 and edit.json()["kind"] == "edit_entry"
    exported = client.get(
        "/exports/diary",
        params={
            "start": (NOW - timedelta(minutes=1)).isoformat(),
            "end": (NOW + timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
        },
        headers=headers,
    )
    assert exported.status_code == 200
    assert exported.json()["rows"][0]["id"] == event_id
    assert db.scalar(select(Event).where(Event.id == event_id)) is not None
