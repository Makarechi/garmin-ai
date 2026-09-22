from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from garmin_ai.accounts import bind_channel, owner
from garmin_ai.api import create_app
from garmin_ai.channels import DeliveryState
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import (
    CustomEntryInput,
    activate_definition,
    create_custom_event,
    propose_definition_revision,
)
from garmin_ai.events import Conflict
from garmin_ai.initiative_rules import (
    TELEGRAM_CONVERSATION_NAMESPACE,
    TRACKER_RULE_NAMESPACE,
    load_rule,
    save_rule,
)
from garmin_ai.models import (
    Conversation,
    Event,
    EventDefinition,
    EventMetricMapping,
    OutboxMessage,
    TrackerConfig,
)
from garmin_ai.proactive import generate_questions
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
        "operation_id": str(uuid4()),
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


def test_generated_edit_preserves_entry_provenance(db):
    install(db)
    event = create_custom_event(
        db,
        CustomEntryInput(
            definition_key="user.focus_session",
            start=NOW,
            end=NOW + timedelta(minutes=25),
            timezone="UTC",
            source="mcp",
            confidence=0.6,
            status="needs_confirmation",
            original_text="synthetic report",
            values={"focus": 4},
            units={"focus": "score_1-5"},
        ),
        actor="mcp",
    )
    action = action_for_event(db, event.id)
    form = form_for_action(db, action.id)
    edited = submit_form(
        db,
        action.id,
        submission(form, action_id=action.id, values={"focus": 5}),
        actor="api",
    )
    assert edited.source == "mcp"
    assert edited.confidence == 0.6
    assert edited.status == "needs_confirmation"
    assert edited.original_text == "synthetic report"


def test_generated_create_form_replays_same_submission(db):
    install(db)
    form = form_for_action(db, available_actions(db)[0].id)
    assert form.submission_id
    first = submit_form(db, form.id, submission(form), actor="test")
    second = submit_form(db, form.id, submission(form), actor="test")
    assert second.id == first.id
    assert list(db.scalars(select(Event))) == [first]


def test_generated_create_form_requires_submission_or_operation_id(db):
    install(db)
    form = form_for_action(db, available_actions(db)[0].id)
    with pytest.raises(ValueError, match="operation or submission ID"):
        submit_form(
            db, form.id, submission(form, submission_id=None, operation_id=None), actor="test"
        )
    assert db.scalar(select(Event.id)) is None


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


def test_enabled_tracker_reminder_is_scheduled_once_per_local_day(db):
    conversation_id = uuid4()
    db.add(
        Conversation(
            id=conversation_id,
            owner_id=owner(db).id,
            channel="restricted-test",
            channel_instance_id="primary",
            external_conversation_id="tracker-reminder-test",
            memory_epoch=uuid4(),
            state={},
        )
    )
    install(
        db,
        focus_draft(
            reminder_enabled=True,
            reminder_time="20:30",
            reminder_timezone="UTC",
        ),
    )
    now = NOW.replace(hour=21)
    generate_questions(db, Settings(timezone="UTC"), now)
    reminders = db.scalars(
        select(OutboxMessage).where(OutboxMessage.state == DeliveryState.QUEUED.value)
    ).all()
    assert len(reminders) == 1
    assert reminders[0].intent["channel_instance"] == {
        "channel": "restricted-test",
        "instance_id": "primary",
    }


def test_paired_tracker_checkin_preserves_consent_and_snooze(db):
    bind_channel(
        db,
        channel="telegram",
        channel_instance_id="primary",
        external_id="42",
        confirmed=True,
    )
    install(db, focus_draft(reminder_timezone="UTC"))
    tracker = db.scalar(select(TrackerConfig))
    now = NOW.replace(hour=21)
    generate_questions(db, Settings(timezone="UTC"), now)

    conversation = db.scalar(select(Conversation))
    assert conversation.id == uuid5(
        TELEGRAM_CONVERSATION_NAMESPACE, f"{owner(db).id}:telegram:primary:42"
    )
    rule_id = uuid5(TRACKER_RULE_NAMESPACE, str(tracker.id))
    rule = load_rule(db, rule_id)
    snoozed_until = now + timedelta(days=2)
    save_rule(db, rule.model_copy(update={"consented": False, "snoozed_until": snoozed_until}))
    generate_questions(db, Settings(timezone="UTC"), now + timedelta(days=1))

    updated = load_rule(db, rule_id)
    assert not updated.consented
    assert updated.snoozed_until == snoozed_until


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
    new_version = activate_definition(
        db, definition.id, proposed.revision, actor="test", authorized=True
    )

    mapped_fields = set(
        db.scalars(
            select(EventMetricMapping.field_id).where(
                EventMetricMapping.event_definition_version_id == new_version.id
            )
        )
    )
    assert mapped_fields == {
        "user.focus_session.focus",
        "user.focus_session.interruptions",
    }

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


def test_generated_form_resolves_local_schema_references(db):
    install(db)
    definition = db.scalar(
        select(EventDefinition).where(EventDefinition.key == "user.focus_session")
    )
    revised = definition_spec(focus_draft())
    focus_schema = revised.payload_schema["properties"]["focus"]
    revised = revised.model_copy(
        update={
            "schema": {
                **revised.payload_schema,
                "$defs": {"focus_score": focus_schema},
                "properties": {
                    **revised.payload_schema["properties"],
                    "focus": {"$ref": "#/$defs/focus_score"},
                },
            }
        }
    )
    proposed = propose_definition_revision(
        db,
        definition.id,
        definition.revision,
        revised,
        actor="test",
        authorized=True,
    )
    activate_definition(db, definition.id, proposed.revision, actor="test", authorized=True)

    form = form_for_action(db, available_actions(db)[0].id)
    focus = next(field for field in form.fields if field.name == "focus")
    assert focus.input == "integer"
    assert focus.minimum == 1
    assert focus.maximum == 5


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
    tracker = confirmed.json()["tracker"]
    changed = client.put(
        f"/tracker-setups/{tracker['id']}/settings",
        json={
            "revision": tracker["revision"],
            "shortcut": tracker["shortcut"],
            "reminder_enabled": False,
            "reminder_time": tracker["reminder_time"],
            "reminder_timezone": tracker["reminder_timezone"],
        },
        headers=headers,
    )
    assert changed.status_code == 200 and changed.json()["reminder_enabled"] is False
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
    submission_body = {
        "action_id": action["id"],
        "operation_id": "dashboard-submit-1",
        "schema_hash": form["schema_hash"],
        "submission_id": form["submission_id"],
        "start": NOW.isoformat(),
        "end": (NOW + timedelta(minutes=25)).isoformat(),
        "timezone": "UTC",
        "values": {"focus": 4},
        "units": {"focus": "score_1-5"},
    }
    response = client.post(
        f"/forms/{action['id']}/submit",
        json=submission_body,
        headers=headers,
    )
    replay = client.post(
        f"/forms/{action['id']}/submit",
        json=submission_body,
        headers=headers,
    )
    assert response.status_code == 200
    assert replay.status_code == 200 and replay.json()["id"] == response.json()["id"]
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
