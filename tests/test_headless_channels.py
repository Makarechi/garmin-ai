from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from garmin_ai.channels import ActionRef, DeliveryState, OutboundIntent, TextBlock
from garmin_ai.restricted_channel import RESTRICTED_INSTANCE, RestrictedTextChannel
from garmin_ai.tracker_forms import (
    FormSubmission,
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    action_for_event,
    available_actions,
    confirm_tracker,
    form_for_action,
    preview_tracker,
    submit_form,
)

NOW = datetime(2026, 9, 20, 18, tzinfo=UTC)


def install_tracker(db):
    draft = TrackerSetupDraft(
        key="focus",
        name="Focus",
        locale="en",
        topology="bounded_interval",
        fields=[
            TrackerFieldDraft(key="quality", label="Quality", kind="scale", minimum=1, maximum=5)
        ],
        shortcut="Log focus",
    )
    preview = preview_tracker(db, draft)
    return confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )


@pytest.mark.parametrize("entry_point", ["api", "telegram", "restricted"])
def test_generated_tracker_create_and_correct_uses_same_application_service(db, entry_point):
    install_tracker(db)
    action = available_actions(db)[0]
    form = form_for_action(db, action.id)
    event = submit_form(
        db,
        action.id,
        FormSubmission(
            action_id=action.id,
            schema_hash=form.schema_hash,
            start=NOW,
            end=NOW + timedelta(minutes=30),
            timezone="UTC",
            values={"quality": 3},
            units={"quality": "score_1-5"},
        ),
        actor=entry_point,
        source="manual" if entry_point != "telegram" else "telegram_text",
        idempotency_key=f"{entry_point}:create",
    )
    edit = action_for_event(db, event.id)
    edit_form = form_for_action(db, edit.id)
    corrected = submit_form(
        db,
        edit.id,
        FormSubmission(
            action_id=edit.id,
            schema_hash=edit_form.schema_hash,
            start=NOW,
            end=NOW + timedelta(minutes=30),
            timezone="UTC",
            values={"quality": 4},
            units={"quality": "score_1-5"},
        ),
        actor=entry_point,
    )
    assert corrected.id == event.id
    assert corrected.revision == 2
    assert corrected.payload["quality"] == 4


@pytest.mark.anyio
async def test_text_fallback_binds_single_use_action_and_receipt_to_context():
    channel = RestrictedTextChannel()
    owner_id, conversation_id, operation_id = uuid4(), uuid4(), uuid4()
    intent = OutboundIntent(
        owner_id=owner_id,
        conversation_id=conversation_id,
        channel_instance=RESTRICTED_INSTANCE,
        blocks=[TextBlock(text="Choose")],
        actions=[
            ActionRef(
                action_id="confirm:v2",
                label="Confirm",
                operation_id=operation_id,
                expires_at=NOW + timedelta(minutes=5),
            )
        ],
    )

    attempt = await channel.deliver(intent, now=NOW)
    assert attempt.state is DeliveryState.PROVIDER_ACCEPTED
    assert not attempt.receipt.confirms_delivery
    token = attempt.rendered.texts[-1].split("[", 1)[1].removesuffix("]")
    assert (
        channel.consume_action(token, owner_id=uuid4(), conversation_id=conversation_id, now=NOW)
        is None
    )
    selected = channel.consume_action(
        token, owner_id=owner_id, conversation_id=conversation_id, now=NOW
    )
    assert selected.operation_id == operation_id
    assert (
        channel.consume_action(token, owner_id=owner_id, conversation_id=conversation_id, now=NOW)
        is None
    )

    receipt = channel.confirm_delivery(attempt.receipt.provider_reference, now=NOW)
    assert receipt.state is DeliveryState.DELIVERED and receipt.confirms_delivery
    assert channel.confirm_delivery(attempt.receipt.provider_reference, now=NOW) is None


def test_headless_ingress_uses_opaque_ids_without_external_sdk():
    channel = RestrictedTextChannel()
    envelope = channel.receive_text(
        owner_id=uuid4(),
        conversation_id=uuid4(),
        external_event_id="not-an-integer/provider-owned",
        sender_ref="opaque-sender",
        text="hello",
        received_at=NOW,
    )
    assert envelope.external_event_id == "not-an-integer/provider-owned"
    assert envelope.external_message_id.startswith("opaque:")
