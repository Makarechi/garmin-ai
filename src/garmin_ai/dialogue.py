"""Channel-neutral inbox, outbox, conversation, and command orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.channels import (
    DeliveryReceipt,
    DeliveryState,
    InboundEnvelope,
    InboundKind,
    OutboundIntent,
)
from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.models import (
    Conversation,
    InboundMessage,
    MessageDeliveryReceipt,
    OutboxMessage,
)


class ActorContext(StrictModel):
    owner_id: UUID
    actor_ref: str = Field(min_length=1, max_length=1000)
    conversation_id: UUID
    channel: str = Field(min_length=1, max_length=100)
    channel_instance_id: str = Field(min_length=1, max_length=200)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    occurred_at: AwareDatetime | None = None
    received_at: AwareDatetime
    operation_id: UUID


class CommandRequest(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


CommandHandler = Callable[[Any, ActorContext, dict[str, Any]], OutboundIntent | None]
MessageHandler = Callable[[Any, ActorContext, InboundEnvelope], OutboundIntent | None]


class CommandDispatcher:
    """Routes semantic command names; transport syntax stays in adapters."""

    def __init__(self) -> None:
        self._handlers: dict[str, tuple[frozenset[str], CommandHandler]] = {}

    def register(
        self,
        name: str,
        handler: CommandHandler,
        *,
        permissions: frozenset[str] = frozenset(),
    ) -> None:
        request = CommandRequest(name=name)
        if request.name in self._handlers:
            raise ValueError(f"Command already registered: {request.name}")
        self._handlers[request.name] = (permissions, handler)

    def dispatch(self, session, actor: ActorContext, request: CommandRequest):
        try:
            required, handler = self._handlers[request.name]
        except KeyError as exc:
            raise LookupError(f"Unknown command: {request.name}") from exc
        if not required <= actor.permissions:
            raise PermissionError("Command permission required")
        return handler(session, actor, request.arguments)


class DialogueResult(StrictModel):
    inbound_message_id: UUID
    operation_id: UUID
    duplicate: bool
    status: str
    outbox_message_id: UUID | None = None


class LegacyOutboundAlias(StrictModel):
    legacy_key: str
    text: str | None = None
    keyboard: Any | None = None


class OutboxLease(StrictModel):
    outbox_message_id: UUID
    lease_token: UUID
    intent: OutboundIntent | LegacyOutboundAlias


def _conversation_for(session, envelope: InboundEnvelope) -> Conversation:
    statement = (
        insert(Conversation)
        .values(
            id=envelope.conversation_id,
            owner_id=envelope.owner_id,
            channel=envelope.channel_instance.channel,
            channel_instance_id=envelope.channel_instance.instance_id,
            external_conversation_id=None,
            memory_epoch=uuid4(),
            state={},
            share_owner_memory=False,
        )
        .on_conflict_do_nothing(index_elements=[Conversation.id])
    )
    session.execute(statement)
    conversation = session.get(Conversation, envelope.conversation_id, populate_existing=True)
    if conversation is None:
        raise RuntimeError("Conversation could not be created")
    expected = (
        envelope.owner_id,
        envelope.channel_instance.channel,
        envelope.channel_instance.instance_id,
    )
    actual = (conversation.owner_id, conversation.channel, conversation.channel_instance_id)
    if actual != expected:
        raise PermissionError("Conversation belongs to another channel binding")
    return conversation


def ingest_envelope(session, envelope: InboundEnvelope) -> tuple[InboundMessage, bool]:
    """Persist an envelope exactly once within its channel-instance namespace."""

    lock_writes(session)
    _conversation_for(session, envelope)
    duplicate = session.scalar(
        select(InboundMessage).where(
            InboundMessage.channel == envelope.channel_instance.channel,
            InboundMessage.channel_instance_id == envelope.channel_instance.instance_id,
            InboundMessage.external_event_id == envelope.external_event_id,
            InboundMessage.revision == envelope.revision,
        )
    )
    if duplicate is not None:
        return duplicate, False
    prior = None
    if envelope.external_message_id is not None:
        prior = session.scalar(
            select(InboundMessage)
            .where(
                InboundMessage.channel == envelope.channel_instance.channel,
                InboundMessage.channel_instance_id == envelope.channel_instance.instance_id,
                InboundMessage.external_message_id == envelope.external_message_id,
            )
            .order_by(InboundMessage.revision.desc())
            .limit(1)
        )
    if envelope.kind is InboundKind.EDIT:
        if prior is None:
            raise Conflict("Edited message has no earlier revision")
        if envelope.revision <= prior.revision:
            raise Conflict("Edited message revision is not newer")

    operation_id = prior.operation_id if prior is not None else uuid4()
    values = {
        "id": envelope.message_id,
        "owner_id": envelope.owner_id,
        "conversation_id": envelope.conversation_id,
        "channel": envelope.channel_instance.channel,
        "channel_instance_id": envelope.channel_instance.instance_id,
        "external_event_id": envelope.external_event_id,
        "external_message_id": envelope.external_message_id,
        "sender_ref": envelope.sender_ref,
        "occurred_at": envelope.occurred_at,
        "received_at": envelope.received_at,
        "kind": envelope.kind.value,
        "normalized_text": envelope.text,
        "envelope": envelope.model_dump(mode="json"),
        "revision": envelope.revision,
        "status": "pending",
        "operation_id": operation_id,
        "supersedes_id": prior.id if prior is not None else None,
    }
    inserted = session.scalar(
        insert(InboundMessage)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_inbound_transport_revision")
        .returning(InboundMessage.id)
    )
    if inserted is not None:
        return session.get(InboundMessage, inserted), True
    existing = session.scalar(
        select(InboundMessage).where(
            InboundMessage.channel == envelope.channel_instance.channel,
            InboundMessage.channel_instance_id == envelope.channel_instance.instance_id,
            InboundMessage.external_event_id == envelope.external_event_id,
            InboundMessage.revision == envelope.revision,
        )
    )
    if existing is None:
        raise RuntimeError("Conflicting ingress was not recoverable")
    return existing, False


def actor_context(row: InboundMessage, permissions=frozenset()) -> ActorContext:
    return ActorContext(
        owner_id=row.owner_id,
        actor_ref=row.sender_ref,
        conversation_id=row.conversation_id,
        channel=row.channel,
        channel_instance_id=row.channel_instance_id,
        permissions=permissions,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        operation_id=row.operation_id,
    )


def queue_intent(
    session,
    intent: OutboundIntent,
    *,
    operation_id: UUID,
    inbound_message_id: UUID | None = None,
    dedup_key: str | None = None,
) -> OutboxMessage:
    """Store delivery intent in the same transaction as the domain mutation."""

    key = dedup_key or f"operation:{operation_id}:reply"
    existing = session.scalar(select(OutboxMessage).where(OutboxMessage.dedup_key == key))
    if existing is not None:
        return existing
    row = OutboxMessage(
        id=intent.intent_id,
        owner_id=intent.owner_id,
        conversation_id=intent.conversation_id,
        inbound_message_id=inbound_message_id,
        operation_id=operation_id,
        intent=intent.model_dump(mode="json"),
        dedup_key=key,
        state=DeliveryState.QUEUED.value,
        attempts=0,
    )
    session.add(row)
    session.flush()
    return row


def claim_outbox(session, now, *, lease_for=timedelta(minutes=2)) -> OutboxLease | None:
    """Claim one queued intent; an abandoned network call becomes uncertain."""

    if now.utcoffset() is None or not timedelta(seconds=1) <= lease_for <= timedelta(hours=1):
        raise ValueError("Outbox lease requires an aware clock and a bounded duration")
    lock_writes(session)
    row = session.scalar(
        select(OutboxMessage)
        .where(
            OutboxMessage.state == DeliveryState.QUEUED.value,
            (OutboxMessage.next_attempt_at.is_(None)) | (OutboxMessage.next_attempt_at <= now),
        )
        .order_by(OutboxMessage.created_at, OutboxMessage.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    token = uuid4()
    row.state = DeliveryState.SENDING.value
    row.lease_token = token
    row.lease_until = now + lease_for
    row.attempts += 1
    session.flush()
    return OutboxLease(
        outbox_message_id=row.id,
        lease_token=token,
        intent=row.intent,
    )


def recover_expired_outbox_leases(session, now) -> int:
    """Fence ambiguous sends instead of assuming failure and retrying."""

    if now.utcoffset() is None:
        raise ValueError("Outbox recovery clock must be aware")
    lock_writes(session)
    rows = session.scalars(
        select(OutboxMessage)
        .where(
            OutboxMessage.state == DeliveryState.SENDING.value,
            OutboxMessage.lease_until < now,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        row.state = DeliveryState.UNCERTAIN.value
        row.lease_until = None
        row.lease_token = None
    session.flush()
    return len(rows)


def explicitly_requeue_uncertain(session, outbox_id: UUID, *, authorized=False) -> OutboxMessage:
    """Require a deliberate reconciliation decision before an ambiguous resend."""

    if not authorized:
        raise PermissionError("Explicit delivery reconciliation permission required")
    row = session.get(OutboxMessage, outbox_id)
    if row is None:
        raise LookupError("Outbox message not found")
    if row.state != DeliveryState.UNCERTAIN.value:
        raise Conflict("Only an uncertain delivery can be explicitly requeued")
    row.state = DeliveryState.QUEUED.value
    row.next_attempt_at = None
    session.flush()
    return row


class DialogueService:
    """Executes one application operation for one deduplicated inbound envelope."""

    def process(
        self,
        session,
        envelope: InboundEnvelope,
        handler: MessageHandler,
        *,
        permissions: frozenset[str] = frozenset(),
    ) -> DialogueResult:
        row, created = ingest_envelope(session, envelope)
        if not created:
            outbox = session.scalar(
                select(OutboxMessage)
                .where(OutboxMessage.inbound_message_id == row.id)
                .order_by(OutboxMessage.created_at, OutboxMessage.id)
                .limit(1)
            )
            return DialogueResult(
                inbound_message_id=row.id,
                operation_id=row.operation_id,
                duplicate=True,
                status=row.status,
                outbox_message_id=outbox.id if outbox else None,
            )

        actor = actor_context(row, permissions)
        intent = handler(session, actor, envelope)
        outbox = None
        if intent is not None:
            expected = (
                row.owner_id,
                row.conversation_id,
                row.channel,
                row.channel_instance_id,
            )
            actual = (
                intent.owner_id,
                intent.conversation_id,
                intent.channel_instance.channel,
                intent.channel_instance.instance_id,
            )
            if actual != expected:
                raise PermissionError("Outbound intent crosses its authenticated conversation")
            outbox = queue_intent(
                session,
                intent,
                operation_id=row.operation_id,
                inbound_message_id=row.id,
                dedup_key=f"operation:{row.operation_id}:revision:{row.revision}:reply",
            )
        row.status = "processed"
        session.flush()
        return DialogueResult(
            inbound_message_id=row.id,
            operation_id=row.operation_id,
            duplicate=False,
            status=row.status,
            outbox_message_id=outbox.id if outbox else None,
        )

    def begin_generation(self, session, conversation_id: UUID) -> UUID:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise LookupError("Conversation not found")
        return conversation.memory_epoch

    def queue_generation_result(
        self,
        session,
        intent: OutboundIntent,
        *,
        expected_epoch: UUID,
        operation_id: UUID,
    ) -> OutboxMessage | None:
        lock_writes(session)
        conversation = session.get(Conversation, intent.conversation_id, populate_existing=True)
        if conversation is None:
            raise LookupError("Conversation not found")
        if conversation.memory_epoch != expected_epoch:
            return None
        return queue_intent(session, intent, operation_id=operation_id)

    def set_pending(self, session, conversation_id: UUID, value: dict[str, Any]) -> None:
        lock_writes(session)
        conversation = session.get(Conversation, conversation_id, populate_existing=True)
        if conversation is None:
            raise LookupError("Conversation not found")
        conversation.state = {**conversation.state, "pending": value}
        session.flush()

    def pending(self, session, conversation_id: UUID) -> dict[str, Any] | None:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise LookupError("Conversation not found")
        return conversation.state.get("pending")

    def forget(self, session, conversation_id: UUID) -> UUID:
        lock_writes(session)
        conversation = session.get(Conversation, conversation_id, populate_existing=True)
        if conversation is None:
            raise LookupError("Conversation not found")
        conversation.memory_epoch = uuid4()
        conversation.state = {}
        session.flush()
        return conversation.memory_epoch


def record_delivery_receipt(
    session,
    outbox_id: UUID,
    receipt: DeliveryReceipt,
    *,
    lease_token: UUID | None = None,
):
    """Record only provider-observed evidence and advance state conservatively."""

    outbox = session.get(OutboxMessage, outbox_id)
    if outbox is None or outbox.id != receipt.intent_id:
        raise LookupError("Outbox message does not match receipt")
    if lease_token is not None and outbox.lease_token != lease_token:
        raise Conflict("Outbox lease was replaced before delivery completed")
    evidence = MessageDeliveryReceipt(
        outbox_message_id=outbox.id,
        state=receipt.state.value,
        observed_at=receipt.observed_at,
        provider_reference=receipt.provider_reference,
        detail=receipt.detail,
    )
    session.add(evidence)
    progress = {
        DeliveryState.QUEUED.value: 0,
        DeliveryState.SENDING.value: 1,
        DeliveryState.PROVIDER_ACCEPTED.value: 2,
        DeliveryState.DELIVERED.value: 3,
        DeliveryState.READ.value: 4,
    }
    if receipt.state.value in progress and progress[receipt.state.value] > progress.get(
        outbox.state, -1
    ):
        outbox.state = receipt.state.value
    elif (
        receipt.state in {DeliveryState.FAILED, DeliveryState.UNCERTAIN}
        and progress.get(outbox.state, -1) < progress[DeliveryState.DELIVERED.value]
    ):
        outbox.state = receipt.state.value
    elif (
        receipt.state in {DeliveryState.CANCELLED, DeliveryState.EXPIRED}
        and progress.get(outbox.state, -1) < progress[DeliveryState.PROVIDER_ACCEPTED.value]
    ):
        outbox.state = receipt.state.value
    if receipt.provider_reference is not None:
        outbox.provider_reference = receipt.provider_reference
    if receipt.state is not DeliveryState.SENDING:
        outbox.lease_until = None
        outbox.lease_token = None
    session.flush()
    return evidence
