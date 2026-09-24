"""Transport-neutral contracts for incoming and outgoing conversations.

Channel adapters own provider authentication, wire-format parsing and network
calls.  Application code exchanges only the models in this module.  External
identifiers are opaque strings and are meaningful only together with their
channel instance.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from garmin_ai.events import StrictModel

OpaqueId = str
TELEGRAM_NAMESPACE = UUID("5ddd62fc-6890-44b6-86a2-20f77524378f")


class ChannelInstanceRef(StrictModel):
    """Stable namespace for provider-controlled opaque identifiers."""

    channel: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=200)

    @property
    def namespace(self) -> tuple[str, str]:
        return (self.channel, self.instance_id)


class ExternalMessageRef(StrictModel):
    channel_instance: ChannelInstanceRef
    external_message_id: OpaqueId = Field(min_length=1, max_length=1000)

    @property
    def identity(self) -> tuple[str, str, str]:
        return (*self.channel_instance.namespace, self.external_message_id)


class AttachmentRef(StrictModel):
    attachment_id: UUID = Field(default_factory=uuid4)
    kind: str = Field(min_length=1, max_length=100)
    media_type: str | None = Field(default=None, min_length=1, max_length=200)
    filename: str | None = Field(default=None, min_length=1, max_length=500)
    size_bytes: int | None = Field(default=None, ge=0)
    external_id: OpaqueId | None = Field(default=None, min_length=1, max_length=1000)


class ActionRef(StrictModel):
    action_id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=300)
    operation_id: UUID
    token: str | None = Field(default=None, min_length=16, max_length=500)
    expires_at: AwareDatetime | None = None


class InboundKind(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    ACTION = "action"
    EDIT = "edit"
    RECEIPT = "receipt"
    SYSTEM = "system"


class InboundEnvelope(StrictModel):
    """Normalized, authenticated input produced by a channel adapter."""

    message_id: UUID = Field(default_factory=uuid4)
    owner_id: UUID
    channel_instance: ChannelInstanceRef
    conversation_id: UUID
    external_event_id: OpaqueId = Field(min_length=1, max_length=1000)
    external_message_id: OpaqueId | None = Field(default=None, min_length=1, max_length=1000)
    sender_ref: OpaqueId = Field(min_length=1, max_length=1000)
    occurred_at: AwareDatetime | None = None
    received_at: AwareDatetime
    time_precision: Literal["exact", "second", "minute", "date", "unknown"] = "unknown"
    kind: InboundKind
    text: str | None = Field(default=None, max_length=100_000)
    reply_to: ExternalMessageRef | None = None
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)
    action: ActionRef | None = None
    revision: int = Field(default=1, ge=1)

    @property
    def ingress_identity(self) -> tuple[str, str, str, int]:
        """Deduplication identity; provider IDs are never parsed as integers."""

        return (*self.channel_instance.namespace, self.external_event_id, self.revision)

    @model_validator(mode="after")
    def validate_kind_payload(self):
        if self.kind is InboundKind.ACTION and self.action is None:
            raise ValueError("action input requires an action reference")
        if self.kind in {InboundKind.TEXT, InboundKind.VOICE} and self.text is None:
            raise ValueError("text and voice input require normalized text")
        return self


class TextBlock(StrictModel):
    text: str = Field(min_length=1, max_length=100_000)


class FormRef(StrictModel):
    definition_key: str = Field(min_length=1, max_length=200)
    definition_version: int = Field(ge=1)


class OutboundIntent(StrictModel):
    """A delivery request without provider-specific rendering instructions."""

    intent_id: UUID = Field(default_factory=uuid4)
    owner_id: UUID
    conversation_id: UUID
    channel_instance: ChannelInstanceRef
    blocks: list[TextBlock] = Field(default_factory=list, max_length=100)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    form: FormRef | None = None
    actions: list[ActionRef] = Field(default_factory=list, max_length=50)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)
    expires_at: AwareDatetime | None = None
    privacy: str = Field(default="private", min_length=1, max_length=50)
    priority: int = Field(default=0, ge=-10, le=10)
    preferred_medium: str = Field(default="text", pattern="^(text|voice)$")
    initiative: bool = False
    policy_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reply_to: ExternalMessageRef | None = None
    replaces: ExternalMessageRef | None = None

    @model_validator(mode="after")
    def has_content(self):
        if not (self.blocks or self.form or self.actions or self.attachments):
            raise ValueError("outbound intent must contain content")
        if any(
            reference is not None and reference.channel_instance != self.channel_instance
            for reference in (self.reply_to, self.replaces)
        ):
            raise ValueError("message reference belongs to another channel instance")
        return self


class ChannelCapabilities(StrictModel):
    text: bool = True
    actions: bool = False
    voice: bool = False
    edit: bool = False
    reply: bool = False
    attachments: bool = False
    initiatives: bool = False
    max_text_length: int = Field(default=4096, ge=1, le=1_000_000)


class DeliveryPolicy(StrictModel):
    """Recipient/context-specific decision evaluated immediately before send."""

    allow_delivery: bool = True
    allow_initiative: bool = True
    reason: str | None = Field(default=None, max_length=1000)
    retry_after: AwareDatetime | None = None


class DeliveryState(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    PROVIDER_ACCEPTED = "provider_accepted"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class DeliveryReceipt(StrictModel):
    """Observed delivery evidence; later states are never inferred from acceptance."""

    receipt_id: UUID = Field(default_factory=uuid4)
    intent_id: UUID
    state: DeliveryState
    observed_at: AwareDatetime
    provider_reference: OpaqueId | None = Field(default=None, min_length=1, max_length=1000)
    detail: str | None = Field(default=None, max_length=2000)

    @property
    def confirms_delivery(self) -> bool:
        return self.state in {DeliveryState.DELIVERED, DeliveryState.READ}

    @property
    def confirms_read(self) -> bool:
        return self.state is DeliveryState.READ


class RenderedDelivery(StrictModel):
    intent_id: UUID
    texts: list[str]
    actions: list[ActionRef] = Field(default_factory=list)
    attachments: list[AttachmentRef] = Field(default_factory=list)
    mode: str = Field(pattern="^(send|edit)$")
    reply_to: ExternalMessageRef | None = None
    related_to: ExternalMessageRef | None = None
    medium: str = Field(pattern="^(text|voice)$")


class DeliveryAttempt(StrictModel):
    intent_id: UUID
    state: DeliveryState
    rendered: RenderedDelivery | None = None
    receipt: DeliveryReceipt | None = None
    reason: str | None = None
    retry_after: AwareDatetime | None = None


class ChannelPort(Protocol):
    """Application-facing port implemented by network and in-memory adapters."""

    @property
    def capabilities(self) -> ChannelCapabilities: ...

    def delivery_policy(self, intent: OutboundIntent, *, now: datetime) -> DeliveryPolicy: ...

    async def deliver(self, intent: OutboundIntent, *, now: datetime) -> DeliveryAttempt: ...


PolicyResolver = Callable[[OutboundIntent, datetime], DeliveryPolicy]


class InMemoryChannel:
    """Deterministic restrictive adapter used to prove capability fallbacks."""

    def __init__(
        self,
        capabilities: ChannelCapabilities | None = None,
        policy_resolver: PolicyResolver | None = None,
    ) -> None:
        self._capabilities = capabilities or ChannelCapabilities()
        self._policy_resolver = policy_resolver
        self.deliveries: list[RenderedDelivery] = []
        self.receipts: list[DeliveryReceipt] = []
        self._action_tokens: dict[str, ActionRef] = {}

    @property
    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    def delivery_policy(self, intent: OutboundIntent, *, now: datetime) -> DeliveryPolicy:
        if self._policy_resolver is not None:
            return self._policy_resolver(intent, now)
        return DeliveryPolicy()

    def _tokenized_actions(self, actions: list[ActionRef], *, now: datetime) -> list[ActionRef]:
        rendered = []
        reserved = set(self._action_tokens)
        for action in actions:
            if action.expires_at is not None and action.expires_at <= now:
                raise ValueError("Cannot render an expired action")
            token = action.token
            while token is None or (token in reserved and action.token is None):
                token = secrets.token_urlsafe(24)
            if token in reserved:
                if self._action_tokens.get(token) == action:
                    rendered.append(action)
                    continue
                raise ValueError("Action token is already active")
            item = action.model_copy(update={"token": token})
            assert item.token is not None
            reserved.add(item.token)
            rendered.append(item)
        self._action_tokens.update(
            {item.token: item for item in rendered if item.token is not None}
        )
        return rendered

    def render(self, intent: OutboundIntent, *, now: datetime | None = None) -> RenderedDelivery:
        now = now or datetime.now(UTC)
        capabilities = self.capabilities
        texts = [block.text for block in intent.blocks]
        actions = self._tokenized_actions(intent.actions, now=now)

        if actions and not capabilities.actions:
            choices = "\n".join(
                f"{index}. {action.label} [{action.token}]"
                for index, action in enumerate(actions, start=1)
            )
            texts.append(choices)
            actions = []

        if intent.form is not None:
            texts.append(f"Form: {intent.form.definition_key} v{intent.form.definition_version}")

        if intent.reply_to is not None and not capabilities.reply:
            texts.insert(0, "Regarding the previous message:")
        if intent.replaces is not None and not capabilities.edit:
            texts.insert(0, "Updated information:")

        medium = intent.preferred_medium if capabilities.voice else "text"
        mode = "edit" if intent.replaces is not None and capabilities.edit else "send"
        related_to = intent.replaces
        reply_to = intent.reply_to if capabilities.reply else None
        attachments = intent.attachments if capabilities.attachments else []

        chunks: list[str] = []
        for text in texts:
            chunks.extend(
                text[offset : offset + capabilities.max_text_length]
                for offset in range(0, len(text), capabilities.max_text_length)
            )
        return RenderedDelivery(
            intent_id=intent.intent_id,
            texts=chunks,
            actions=actions,
            attachments=attachments,
            mode=mode,
            reply_to=reply_to,
            related_to=related_to,
            medium=medium,
        )

    async def deliver(self, intent: OutboundIntent, *, now: datetime) -> DeliveryAttempt:
        if intent.expires_at is not None and intent.expires_at <= now:
            return DeliveryAttempt(intent_id=intent.intent_id, state=DeliveryState.EXPIRED)
        if any(
            action.expires_at is not None and action.expires_at <= now for action in intent.actions
        ):
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.EXPIRED,
                reason="outbound action expired before delivery",
            )
        policy = self.delivery_policy(intent, now=now)
        if not policy.allow_delivery or (
            intent.initiative and (not policy.allow_initiative or not self.capabilities.initiatives)
        ):
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.QUEUED,
                reason=policy.reason or "channel cannot initiate delivery in this context",
                retry_after=policy.retry_after,
            )

        needs_text = bool(
            intent.blocks
            or intent.form
            or (intent.actions and not self.capabilities.actions)
            or (intent.reply_to is not None and not self.capabilities.reply)
            or (intent.replaces is not None and not self.capabilities.edit)
        )
        voice_delivery = intent.preferred_medium == "voice" and self.capabilities.voice
        if needs_text and not (self.capabilities.text or voice_delivery):
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.QUEUED,
                reason="channel cannot represent the requested content",
            )
        if intent.attachments and not self.capabilities.attachments:
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.QUEUED,
                reason="channel cannot deliver the requested attachments",
            )

        rendered = self.render(intent, now=now)
        self.deliveries.append(rendered)
        receipt = DeliveryReceipt(
            intent_id=intent.intent_id,
            state=DeliveryState.PROVIDER_ACCEPTED,
            observed_at=now,
            provider_reference=str(len(self.deliveries)),
        )
        self.receipts.append(receipt)
        return DeliveryAttempt(
            intent_id=intent.intent_id,
            state=DeliveryState.PROVIDER_ACCEPTED,
            rendered=rendered,
            receipt=receipt,
        )

    def consume_action(self, token: str, *, now: datetime) -> ActionRef | None:
        action = self._action_tokens.pop(token, None)
        if action is not None and action.expires_at is not None and action.expires_at <= now:
            return None
        return action
