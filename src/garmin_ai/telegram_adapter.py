"""Telegram transport adapter for the channel-neutral messaging contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, RetryAfter

from garmin_ai.accounts import owner
from garmin_ai.channels import (
    ActionRef,
    AttachmentRef,
    ChannelCapabilities,
    ChannelInstanceRef,
    DeliveryAttempt,
    DeliveryPolicy,
    DeliveryReceipt,
    DeliveryState,
    ExternalMessageRef,
    InboundEnvelope,
    InboundKind,
    InMemoryChannel,
    OutboundIntent,
)
from garmin_ai.dialogue import ingest_envelope
from garmin_ai.models import InboundMessage, TelegramUpdate

TELEGRAM_NAMESPACE = UUID("5ddd62fc-6890-44b6-86a2-20f77524378f")
TELEGRAM_INSTANCE = ChannelInstanceRef(channel="telegram", instance_id="primary")


def authenticated_message(update: dict, owner_id: int):
    """Authenticate the sender and private chat before exposing normalized input."""

    if owner_id <= 0:
        return None
    callback = update.get("callback_query")
    message = (
        callback.get("message", {})
        if callback
        else update.get("message", update.get("edited_message", {}))
    )
    sender = callback.get("from", {}) if callback else message.get("from", {})
    chat = message.get("chat", {})
    if sender.get("id") != owner_id or chat.get("id") != owner_id or chat.get("type") != "private":
        return None
    return message


def _occurred_at(message: dict) -> datetime | None:
    value = message.get("edit_date", message.get("date"))
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, UTC)


def normalize_update(
    update: dict,
    *,
    external_owner_id: int,
    internal_owner_id: UUID,
    received_at: datetime,
) -> InboundEnvelope:
    """Convert one already authenticated provider update into the neutral contract."""

    message = authenticated_message(update, external_owner_id)
    if message is None:
        raise PermissionError("Telegram update is not owned by the configured private user")
    callback = update.get("callback_query")
    edited = update.get("edited_message") is not None
    external_message_id = message.get("message_id")
    chat_id = str(message["chat"]["id"])
    update_id = str(update["update_id"])
    conversation_id = uuid5(
        TELEGRAM_NAMESPACE,
        f"{internal_owner_id}:telegram:primary:{chat_id}",
    )
    operation_id = uuid5(TELEGRAM_NAMESPACE, f"action:{update_id}")
    action = None
    attachments = []
    text = message.get("text") or message.get("caption")
    kind = InboundKind.TEXT
    if callback:
        kind = InboundKind.ACTION
        action = ActionRef(
            action_id=str(callback.get("data") or "legacy"),
            label=str(callback.get("data") or "legacy action"),
            operation_id=operation_id,
        )
    elif edited:
        kind = InboundKind.EDIT
    elif message.get("voice"):
        voice = message["voice"]
        kind = InboundKind.VOICE
        text = text or ""
        attachments.append(
            AttachmentRef(
                kind="voice",
                media_type=voice.get("mime_type") or "audio/ogg",
                size_bytes=voice.get("file_size"),
                external_id=str(voice["file_id"]),
            )
        )
    reply_to = None
    if message.get("reply_to_message", {}).get("message_id") is not None:
        reply_to = ExternalMessageRef(
            channel_instance=TELEGRAM_INSTANCE,
            external_message_id=str(message["reply_to_message"]["message_id"]),
        )
    revision = int(message.get("edit_date") or 1) if edited else 1
    return InboundEnvelope(
        owner_id=internal_owner_id,
        channel_instance=TELEGRAM_INSTANCE,
        conversation_id=conversation_id,
        external_event_id=update_id,
        external_message_id=str(external_message_id) if external_message_id is not None else None,
        sender_ref=str(external_owner_id),
        occurred_at=_occurred_at(message),
        received_at=received_at,
        time_precision="second" if _occurred_at(message) is not None else "unknown",
        kind=kind,
        text=text,
        reply_to=reply_to,
        attachments=attachments,
        action=action,
        revision=revision,
    )


def record_neutral_ingress(session, update: dict, owner_id: int, received_at: datetime):
    """Dual-write authenticated ingress while the legacy dispatcher remains the sole consumer."""

    person = owner(session)
    envelope = normalize_update(
        update,
        external_owner_id=owner_id,
        internal_owner_id=person.id,
        received_at=received_at,
    )
    row, created = ingest_envelope(session, envelope)
    if row.legacy_telegram_update_id is None:
        row.legacy_telegram_update_id = int(update["update_id"])
    return row, created


def set_update_status(session, update_id: int, status: str) -> None:
    """Keep the compatibility row and neutral inbox lifecycle aligned."""

    legacy = session.get(TelegramUpdate, update_id)
    if legacy is not None:
        legacy.status = status
    neutral = session.scalar(
        select(InboundMessage).where(InboundMessage.legacy_telegram_update_id == update_id)
    )
    if neutral is not None:
        neutral.status = status


PolicyResolver = Callable[[OutboundIntent, datetime], DeliveryPolicy]


class TelegramChannel:
    """Render neutral intents without leaking Telegram objects into domain services."""

    def __init__(self, bot, chat_id: int, policy_resolver: PolicyResolver | None = None):
        self.bot = bot
        self.chat_id = chat_id
        self.policy_resolver = policy_resolver
        self._renderer = InMemoryChannel(self.capabilities)

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            text=True,
            actions=True,
            voice=False,
            edit=False,
            reply=False,
            attachments=False,
            initiatives=True,
            max_text_length=3500,
        )

    def delivery_policy(self, intent: OutboundIntent, *, now: datetime) -> DeliveryPolicy:
        if self.policy_resolver is None:
            return DeliveryPolicy()
        return self.policy_resolver(intent, now)

    async def deliver(self, intent: OutboundIntent, *, now: datetime) -> DeliveryAttempt:
        if intent.channel_instance != TELEGRAM_INSTANCE:
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.FAILED,
                reason="intent targets another channel instance",
            )
        if intent.expires_at is not None and intent.expires_at <= now:
            return DeliveryAttempt(intent_id=intent.intent_id, state=DeliveryState.EXPIRED)
        policy = self.delivery_policy(intent, now=now)
        if not policy.allow_delivery or (intent.initiative and not policy.allow_initiative):
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.QUEUED,
                reason=policy.reason or "Telegram delivery is disabled by policy",
                retry_after=policy.retry_after,
            )
        if intent.attachments:
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.QUEUED,
                reason="Telegram attachment delivery is not implemented",
            )
        rendered = self._renderer.render(intent)
        texts = rendered.texts or ["Выберите действие:"]
        buttons = [
            [InlineKeyboardButton(action.label, callback_data=action.token)]
            for action in rendered.actions
        ]
        provider_reference = None
        try:
            for index, text in enumerate(texts):
                message = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=None,
                    reply_markup=InlineKeyboardMarkup(buttons) if buttons and index == 0 else None,
                )
                provider_reference = str(message.message_id)
        except RetryAfter as exc:
            seconds = (
                exc.retry_after.total_seconds()
                if isinstance(exc.retry_after, timedelta)
                else exc.retry_after
            )
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.QUEUED,
                rendered=rendered,
                reason="Telegram rate limit",
                retry_after=now + timedelta(seconds=seconds),
            )
        except BadRequest:
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.FAILED,
                rendered=rendered,
                reason="Telegram rejected the rendered message",
            )
        except NetworkError:
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.UNCERTAIN,
                rendered=rendered,
                reason="Telegram send outcome is unknown",
            )
        receipt = DeliveryReceipt(
            intent_id=intent.intent_id,
            state=DeliveryState.PROVIDER_ACCEPTED,
            observed_at=now,
            provider_reference=provider_reference,
        )
        return DeliveryAttempt(
            intent_id=intent.intent_id,
            state=DeliveryState.PROVIDER_ACCEPTED,
            rendered=rendered,
            receipt=receipt,
        )
