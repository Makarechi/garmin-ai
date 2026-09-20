from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from garmin_ai.channels import (
    ActionRef,
    AttachmentRef,
    ChannelCapabilities,
    ChannelInstanceRef,
    DeliveryPolicy,
    DeliveryState,
    ExternalMessageRef,
    InboundEnvelope,
    InboundKind,
    InMemoryChannel,
    OutboundIntent,
    TextBlock,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def envelope(instance: ChannelInstanceRef) -> InboundEnvelope:
    return InboundEnvelope(
        owner_id=uuid4(),
        channel_instance=instance,
        conversation_id=uuid4(),
        external_event_id="123",
        external_message_id="123",
        sender_ref="123",
        occurred_at=None,
        received_at=NOW,
        kind=InboundKind.TEXT,
        text="hello",
    )


def intent(**updates) -> OutboundIntent:
    values = {
        "owner_id": uuid4(),
        "conversation_id": uuid4(),
        "channel_instance": ChannelInstanceRef(channel="test", instance_id="restricted"),
        "blocks": [TextBlock(text="Choose")],
    }
    values.update(updates)
    return OutboundIntent(**values)


def test_opaque_external_ids_are_namespaced_by_channel_instance():
    first = envelope(ChannelInstanceRef(channel="telegram", instance_id="personal"))
    second = envelope(ChannelInstanceRef(channel="test", instance_id="personal"))

    assert first.ingress_identity != second.ingress_identity
    assert first.external_event_id == second.external_event_id == "123"


@pytest.mark.anyio
async def test_restrictive_channel_preserves_actions_edit_reply_and_voice_semantics():
    operation_id = uuid4()
    previous = ExternalMessageRef(
        channel_instance=ChannelInstanceRef(channel="test", instance_id="restricted"),
        external_message_id="opaque:previous",
    )
    channel = InMemoryChannel(
        ChannelCapabilities(
            actions=False,
            edit=False,
            reply=False,
            voice=False,
            attachments=False,
            max_text_length=1000,
        )
    )
    outbound = intent(
        preferred_medium="voice",
        reply_to=previous,
        replaces=previous,
        actions=[ActionRef(action_id="confirm", label="Confirm", operation_id=operation_id)],
    )

    attempt = await channel.deliver(outbound, now=NOW)

    assert attempt.state is DeliveryState.PROVIDER_ACCEPTED
    assert attempt.rendered is not None
    assert attempt.rendered.mode == "send"
    assert attempt.rendered.medium == "text"
    assert attempt.rendered.reply_to is None
    assert attempt.rendered.related_to == previous
    assert attempt.rendered.actions == []
    assert attempt.rendered.texts[0] == "Updated information:"
    assert attempt.rendered.texts[1] == "Regarding the previous message:"
    assert attempt.rendered.texts[-1].startswith("1. Confirm [")

    token = attempt.rendered.texts[-1].removeprefix("1. Confirm [").removesuffix("]")
    assert channel.consume_action(token, now=NOW).operation_id == operation_id
    assert channel.consume_action(token, now=NOW) is None


@pytest.mark.anyio
async def test_provider_acceptance_is_not_delivery_or_read_evidence():
    channel = InMemoryChannel()

    attempt = await channel.deliver(intent(), now=NOW)

    assert attempt.receipt is not None
    assert attempt.receipt.state is DeliveryState.PROVIDER_ACCEPTED
    assert not attempt.receipt.confirms_delivery
    assert not attempt.receipt.confirms_read


@pytest.mark.anyio
async def test_dynamic_policy_keeps_blocked_initiative_pending():
    retry_at = NOW + timedelta(hours=2)
    channel = InMemoryChannel(
        policy_resolver=lambda _intent, _now: DeliveryPolicy(
            allow_delivery=True,
            allow_initiative=False,
            reason="quiet hours",
            retry_after=retry_at,
        ),
        capabilities=ChannelCapabilities(initiatives=True),
    )

    attempt = await channel.deliver(intent(initiative=True), now=NOW)

    assert attempt.state is DeliveryState.QUEUED
    assert attempt.reason == "quiet hours"
    assert attempt.retry_after == retry_at
    assert channel.deliveries == []


@pytest.mark.anyio
async def test_static_capabilities_can_keep_an_initiative_pending():
    channel = InMemoryChannel(ChannelCapabilities(initiatives=False))

    attempt = await channel.deliver(intent(initiative=True), now=NOW)

    assert attempt.state is DeliveryState.QUEUED
    assert "cannot initiate" in attempt.reason
    assert channel.deliveries == []


@pytest.mark.anyio
async def test_unsupported_attachment_is_queued_instead_of_silently_dropped():
    channel = InMemoryChannel(ChannelCapabilities(attachments=False))

    attempt = await channel.deliver(
        intent(attachments=[AttachmentRef(kind="document", filename="report.txt")]),
        now=NOW,
    )

    assert attempt.state is DeliveryState.QUEUED
    assert "attachments" in attempt.reason
    assert channel.deliveries == []


def test_neutral_contract_has_no_telegram_wire_types():
    source = (Path(__file__).parents[1] / "src/garmin_ai/channels.py").read_text()

    for forbidden in ("InlineKeyboardMarkup", "CallbackQuery", "parse_mode", "telegram.Bot"):
        assert forbidden not in source
