import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from garmin_ai.accounts import bind_channel, owner
from garmin_ai.action_tokens import consume_action_token, issue_action_token
from garmin_ai.channels import ChannelInstanceRef, OutboundIntent, TextBlock
from garmin_ai.config import ApiToken
from garmin_ai.definitions import DefinitionSpec, FieldSpec
from garmin_ai.dialogue import queue_intent
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import Conversation
from garmin_ai.natural_language import process_tracker_text
from garmin_ai.pack_export import export_tracker_pack
from garmin_ai.share_policy import TrackerShareConsent, grant_tracker_share
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

NOW = datetime(2026, 9, 20, 18, tzinfo=UTC)


def sensitive_tracker(db):
    draft = TrackerSetupDraft(
        key="symptom",
        name="Symptom",
        locale="en",
        privacy="sensitive",
        fields=[
            TrackerFieldDraft(key="severity", label="Severity", kind="scale", minimum=1, maximum=5)
        ],
    )
    preview = preview_tracker(db, draft)
    return confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )


def test_schema_profile_rejects_remote_refs_callbacks_and_code_hooks():
    base = dict(
        key="user.malicious",
        labels={"en": "Malicious"},
        fields={
            "value": FieldSpec(id="user.malicious.value", labels={"en": "Value"}, semantic="text")
        },
        topology="point",
    )
    for node in (
        {"$ref": "https://example.invalid/schema.json"},
        {"type": "string", "maxLength": 20, "callback": "https://example.invalid"},
        {"type": "string", "maxLength": 20, "code": "open('/etc/passwd').read()"},
    ):
        with pytest.raises((ValidationError, ValueError)):
            DefinitionSpec(
                **base,
                schema={
                    "type": "object",
                    "properties": {"value": node},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            )


def test_sensitive_tracker_needs_separate_model_and_channel_consent(db):
    created = sensitive_tracker(db)
    definition_id = created["tracker"]["definition_id"]

    class ForbiddenProvider:
        def structured(self, *_args, **_kwargs):
            raise AssertionError("Sensitive schema or facts reached the model")

    result = process_tracker_text(
        db,
        ForbiddenProvider(),
        {
            "text": "severity 4 at 18:00",
            "operation_id": "sensitive-1",
            "selected_definition_version_id": created["action"]["definition_version_id"],
        },
        granted={"read:diary", "write:diary"},
        actor="test",
        now=NOW,
        timezone="UTC",
    )
    assert result["reason"] == "sensitive_tracker_consent_required"

    conversation_id = uuid4()
    db.add(
        Conversation(
            id=conversation_id,
            owner_id=owner(db).id,
            channel="restricted-test",
            channel_instance_id="primary",
            external_conversation_id="opaque",
            memory_epoch=uuid4(),
            state={},
        )
    )
    intent = OutboundIntent(
        owner_id=owner(db).id,
        conversation_id=conversation_id,
        channel_instance=ChannelInstanceRef(channel="restricted-test", instance_id="primary"),
        blocks=[TextBlock(text="Sensitive check-in")],
        evidence_refs=[f"definition:{definition_id}"],
    )
    with pytest.raises(PermissionError, match="consent"):
        queue_intent(db, intent, operation_id=uuid4())

    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=definition_id,
            destination_kind="channel",
            destination_instance_id="restricted-test:primary",
            categories={"schema", "facts"},
            granted_at=NOW,
        ),
        authorized=True,
    )
    assert queue_intent(db, intent, operation_id=uuid4()) is not None


def test_sensitive_tracker_consent_requires_unambiguous_time():
    with pytest.raises(ValidationError):
        TrackerShareConsent(
            definition_id=uuid4(),
            destination_kind="model",
            destination_instance_id="model:synthetic",
            categories={"schema"},
            granted_at=datetime(2026, 9, 21),
        )


def test_pack_export_contains_contracts_but_no_facts_bindings_or_messages(db):
    created = sensitive_tracker(db)
    definition_id = created["tracker"]["definition_id"]
    bind_channel(
        db,
        channel="telegram",
        channel_instance_id="primary",
        external_id="secret-owner-id",
        confirmed=True,
    )
    create_event(
        db,
        EventInput(
            start=NOW,
            timezone="UTC",
            source="manual",
            original_text="private original message",
            payload={"type": "note", "description": "private fact"},
        ),
        actor="test",
    )
    exported = export_tracker_pack(db, [definition_id])
    encoded = json.dumps(exported)

    assert exported["format"] == "garmin-ai-tracker-pack-v1"
    assert exported["trackers"][0]["versions"][0]["privacy"] == "sensitive"
    for forbidden in (
        "private original message",
        "private fact",
        "secret-owner-id",
        "owner_id",
        "channel_binding",
        "token",
    ):
        assert forbidden not in encoded


def test_action_token_is_signed_expiring_context_bound_and_single_use(db):
    key = b"synthetic-action-key-that-is-at-least-32-bytes"
    owner_id, conversation_id = uuid4(), uuid4()
    clock = datetime.now(UTC)
    token = issue_action_token(
        db,
        key,
        owner_id=owner_id,
        conversation_id=conversation_id,
        action_id="confirm:v2",
        revision=2,
        expires_at=clock + timedelta(minutes=5),
    )

    assert (
        consume_action_token(
            db,
            key,
            token,
            owner_id=uuid4(),
            conversation_id=conversation_id,
            revision=2,
            now=clock,
        )
        is None
    )
    assert (
        consume_action_token(
            db,
            key,
            token + "x",
            owner_id=owner_id,
            conversation_id=conversation_id,
            revision=2,
            now=clock,
        )
        is None
    )
    assert (
        consume_action_token(
            db,
            key,
            token,
            owner_id=owner_id,
            conversation_id=conversation_id,
            revision=2,
            now=clock,
        )
        == "confirm:v2"
    )
    assert (
        consume_action_token(
            db,
            key,
            token,
            owner_id=owner_id,
            conversation_id=conversation_id,
            revision=2,
            now=clock,
        )
        is None
    )


def test_definition_and_integration_permissions_are_distinct():
    definition = ApiToken(key="d" * 40, scopes={"manage:definitions"})
    integration = ApiToken(key="i" * 40, scopes={"manage:integrations"})
    assert definition.scopes != integration.scopes
