"""Deterministic text-only channel used to prove the neutral adapter boundary."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, cast, delete, select
from sqlalchemy.orm import Session

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
from garmin_ai.models import AppState, InboundMessage

RESTRICTED_INSTANCE = ChannelInstanceRef(channel="restricted-test", instance_id="primary")


class RestrictedTextChannel:
    """No buttons, edits, replies, voice, files, or synchronous delivery receipts."""

    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
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
        self._session_factory = session_factory
        self.deliveries = self._renderer.deliveries

    @staticmethod
    def _storage_key(kind: str, reference: str) -> str:
        return f"restricted-{kind}:" + hashlib.sha256(reference.encode()).hexdigest()

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
            if action.expires_at is not None and action.expires_at <= now:
                raise ValueError("Cannot render an expired action")
            token = secrets.token_urlsafe(24)
            rendered = action.model_copy(
                update={
                    "token": token,
                    "expires_at": action.expires_at or now + timedelta(minutes=15),
                }
            )
            if self._session_factory is None:
                self._actions[token] = (intent.owner_id, intent.conversation_id, rendered)
            actions.append(rendered)
        provider_reference = "opaque:" + uuid4().hex
        if self._session_factory is None:
            self._pending_receipts[provider_reference] = intent.intent_id
        else:
            # Commit token and receipt state before the synthetic provider can
            # accept the message; a restart cannot strand a displayed token.
            with self._session_factory() as session:
                session.execute(
                    delete(AppState).where(
                        AppState.key.startswith("restricted-action:"),
                        cast(AppState.value["expires_epoch_us"].astext, BigInteger)
                        <= int(now.timestamp() * 1_000_000),
                    )
                )
                for action in actions:
                    session.add(
                        AppState(
                            key=self._storage_key("action", action.token),
                            value={
                                "owner_id": str(intent.owner_id),
                                "conversation_id": str(intent.conversation_id),
                                "action": action.model_dump(mode="json"),
                                "expires_epoch_us": int(action.expires_at.timestamp() * 1_000_000),
                            },
                        )
                    )
                session.add(
                    AppState(
                        key=self._storage_key("receipt", provider_reference),
                        value={"intent_id": str(intent.intent_id)},
                    )
                )
                session.commit()
        attempt = await self._renderer.deliver(
            intent.model_copy(update={"actions": actions}),
            now=now,
        )
        if attempt.state is not DeliveryState.PROVIDER_ACCEPTED:
            if self._session_factory is None:
                for action in actions:
                    self._actions.pop(action.token, None)
                self._pending_receipts.pop(provider_reference, None)
            else:
                with self._session_factory() as session:
                    for action in actions:
                        row = session.get(AppState, self._storage_key("action", action.token))
                        if row is not None:
                            session.delete(row)
                    receipt = session.get(
                        AppState, self._storage_key("receipt", provider_reference)
                    )
                    if receipt is not None:
                        session.delete(receipt)
                    session.commit()
            return attempt
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
        session: Session | None = None,
    ) -> ActionRef | None:
        if self._session_factory is not None:
            if session is not None:
                return self._consume_persisted_action(
                    session, token, owner_id=owner_id, conversation_id=conversation_id, now=now
                )
            with self._session_factory() as owned_session:
                action = self._consume_persisted_action(
                    owned_session,
                    token,
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    now=now,
                )
                owned_session.commit()
                return action
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

    def _consume_persisted_action(
        self, session: Session, token: str, *, owner_id: UUID, conversation_id: UUID, now: datetime
    ) -> ActionRef | None:
        row = session.get(AppState, self._storage_key("action", token), with_for_update=True)
        if (
            row is None
            or row.value.get("owner_id") != str(owner_id)
            or row.value.get("conversation_id") != str(conversation_id)
        ):
            return None
        action = ActionRef.model_validate(row.value["action"])
        session.delete(row)
        session.flush()
        if action.expires_at is not None and action.expires_at <= now:
            return None
        return action

    def confirm_delivery(
        self, provider_reference: str, *, now: datetime, session: Session | None = None
    ) -> DeliveryReceipt | None:
        if self._session_factory is not None:
            if session is None:
                raise ValueError("Persisted delivery receipt requires the caller's transaction")
            row = session.get(
                AppState,
                self._storage_key("receipt", provider_reference),
                with_for_update=True,
            )
            if row is None:
                return None
            intent_id = UUID(row.value["intent_id"])
            session.delete(row)
            session.flush()
        else:
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

    def receive_action_token(
        self,
        *,
        owner_id: UUID,
        conversation_id: UUID,
        external_event_id: str,
        sender_ref: str,
        token: str,
        received_at: datetime,
        session: Session | None = None,
    ) -> InboundEnvelope:
        """Resolve a text fallback token before it reaches the semantic consumer."""

        if self._session_factory is not None:
            if session is None:
                raise ValueError("Persisted action ingress requires the caller's transaction")
            existing = session.scalar(
                select(InboundMessage).where(
                    InboundMessage.channel == RESTRICTED_INSTANCE.channel,
                    InboundMessage.channel_instance_id == RESTRICTED_INSTANCE.instance_id,
                    InboundMessage.external_event_id == external_event_id,
                    InboundMessage.revision == 1,
                )
            )
            if existing is not None:
                envelope = InboundEnvelope.model_validate(existing.envelope)
                if (
                    existing.owner_id != owner_id
                    or existing.conversation_id != conversation_id
                    or existing.sender_ref != sender_ref
                    or envelope.kind is not InboundKind.ACTION
                    or envelope.action is None
                    or envelope.action.token != token
                ):
                    raise PermissionError("Reference ingress event conflicts with its prior action")
                return envelope

        action = self.consume_action(
            token,
            owner_id=owner_id,
            conversation_id=conversation_id,
            now=received_at,
            session=session,
        )
        if action is None:
            raise LookupError("Reference action is unavailable or already used")
        return InboundEnvelope(
            owner_id=owner_id,
            channel_instance=RESTRICTED_INSTANCE,
            conversation_id=conversation_id,
            external_event_id=external_event_id,
            external_message_id="opaque:" + uuid4().hex,
            sender_ref=sender_ref,
            occurred_at=None,
            received_at=received_at,
            kind=InboundKind.ACTION,
            action=action,
        )
