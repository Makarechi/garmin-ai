"""Deterministic text-only channel used to prove the neutral adapter boundary."""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID, uuid4

from garmin_ai.channels import (
    ActionRef,
    ChannelCapabilities,
    ChannelInstanceRef,
    DeliveryAttempt,
    DeliveryReceipt,
    DeliveryState,
    InboundEnvelope,
    InboundKind,
    InMemoryChannel,
    OutboundIntent,
)

RESTRICTED_INSTANCE = ChannelInstanceRef(channel="restricted-test", instance_id="primary")


class RestrictedTextChannel:
    """No buttons, edits, replies, voice, files, or synchronous delivery receipts."""

    def __init__(self) -> None:
        self._renderer = InMemoryChannel(
            ChannelCapabilities(
                text=True,
                actions=False,
                voice=False,
                edit=False,
                reply=False,
                attachments=False,
                initiatives=True,
                max_text_length=320,
            )
        )
        self._actions: dict[str, tuple[UUID, UUID, ActionRef]] = {}
        self._pending_receipts: dict[str, UUID] = {}
        self.deliveries = self._renderer.deliveries

    @property
    def capabilities(self) -> ChannelCapabilities:
        return self._renderer.capabilities

    def delivery_policy(self, intent: OutboundIntent, *, now: datetime):
        return self._renderer.delivery_policy(intent, now=now)

    async def deliver(self, intent: OutboundIntent, *, now: datetime) -> DeliveryAttempt:
        if intent.channel_instance != RESTRICTED_INSTANCE:
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.FAILED,
                reason="intent targets another channel instance",
            )
        if intent.attachments:
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.QUEUED,
                reason="Restricted channel attachment delivery is not implemented",
            )
        actions = []
        for action in intent.actions:
            token = secrets.token_urlsafe(24)
            rendered = action.model_copy(update={"token": token})
            self._actions[token] = (intent.owner_id, intent.conversation_id, rendered)
            actions.append(rendered)
        attempt = await self._renderer.deliver(
            intent.model_copy(update={"actions": actions}),
            now=now,
        )
        if attempt.state is not DeliveryState.PROVIDER_ACCEPTED:
            return attempt
        provider_reference = "opaque:" + uuid4().hex
        self._pending_receipts[provider_reference] = intent.intent_id
        return attempt.model_copy(
            update={
                "receipt": attempt.receipt.model_copy(
                    update={"provider_reference": provider_reference}
                )
            }
        )

    def consume_action(
        self,
        token: str,
        *,
        owner_id: UUID,
        conversation_id: UUID,
        now: datetime,
    ) -> ActionRef | None:
        bound = self._actions.get(token)
        if bound is None:
            return None
        expected_owner, expected_conversation, action = bound
        if owner_id != expected_owner or conversation_id != expected_conversation:
            return None
        self._actions.pop(token, None)
        if action.expires_at is not None and action.expires_at <= now:
            return None
        return action

    def confirm_delivery(self, provider_reference: str, *, now: datetime) -> DeliveryReceipt | None:
        intent_id = self._pending_receipts.pop(provider_reference, None)
        if intent_id is None:
            return None
        return DeliveryReceipt(
            intent_id=intent_id,
            state=DeliveryState.DELIVERED,
            observed_at=now,
            provider_reference=provider_reference,
        )

    def receive_text(
        self,
        *,
        owner_id: UUID,
        conversation_id: UUID,
        external_event_id: str,
        sender_ref: str,
        text: str,
        received_at: datetime,
    ) -> InboundEnvelope:
        return InboundEnvelope(
            owner_id=owner_id,
            channel_instance=RESTRICTED_INSTANCE,
            conversation_id=conversation_id,
            external_event_id=external_event_id,
            external_message_id="opaque:" + uuid4().hex,
            sender_ref=sender_ref,
            occurred_at=None,
            received_at=received_at,
            kind=InboundKind.TEXT,
            text=text,
        )
