"""Telegram transport adapter for the channel-neutral messaging contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
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
from garmin_ai.models import AppState, InboundMessage, TelegramUpdate

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
    resolved_action: ActionRef | None = None,
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
        action = resolved_action or ActionRef(
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
    existing = session.scalar(
        select(InboundMessage).where(
            InboundMessage.legacy_telegram_update_id == int(update["update_id"])
        )
    )
    if existing is not None:
        return existing, False
    resolved_action = None
    callback = update.get("callback_query")
    if callback and callback.get("data"):
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id"))
        conversation_id = uuid5(
            TELEGRAM_NAMESPACE,
            f"{person.id}:telegram:primary:{chat_id}",
        )
        resolved_action = resolve_telegram_action(
            session,
            str(callback["data"]),
            owner_id=person.id,
            conversation_id=conversation_id,
            now=received_at,
        )
    envelope = normalize_update(
        update,
        external_owner_id=owner_id,
        internal_owner_id=person.id,
        received_at=received_at,
        resolved_action=resolved_action,
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
ActionRecorder = Callable[[OutboundIntent, list[ActionRef], datetime], None]


def _action_key(token: str) -> str:
    return "channel-action:telegram:" + sha256(token.encode()).hexdigest()


def persist_telegram_actions(
    session,
    intent: OutboundIntent,
    actions: list[ActionRef],
    now: datetime,
) -> None:
    """Persist opaque callback authorization without storing the token in plaintext."""

    for action in actions:
        if action.token is None:
            raise ValueError("Rendered Telegram action requires a token")
        key = _action_key(action.token)
        existing = session.get(AppState, key)
        value = {
            "owner_id": str(intent.owner_id),
            "conversation_id": str(intent.conversation_id),
            "action": action.model_dump(mode="json", exclude={"token"}),
            "created_at": now.isoformat(),
            "consumed_at": None,
        }
        if existing is not None:
            if any(
                existing.value.get(field) != value[field]
                for field in ("owner_id", "conversation_id", "action")
            ):
                raise ValueError("Telegram action token collision")
            continue
        session.add(AppState(key=key, value=value))
    session.flush()


def resolve_telegram_action(
    session,
    token: str,
    *,
    owner_id: UUID,
    conversation_id: UUID,
    now: datetime,
) -> ActionRef | None:
    row = session.get(AppState, _action_key(token), populate_existing=True)
    if row is None:
        return None
    value = row.value
    action = ActionRef.model_validate(value["action"])
    if (
        value.get("owner_id") != str(owner_id)
        or value.get("conversation_id") != str(conversation_id)
        or value.get("consumed_at") is not None
        or (action.expires_at is not None and action.expires_at <= now)
    ):
        raise PermissionError("Telegram action is expired, consumed, or belongs elsewhere")
    row.value = {**value, "consumed_at": now.isoformat()}
    session.flush()
    return action


class TelegramChannel:
    """Render neutral intents without leaking Telegram objects into domain services."""

    def __init__(
        self,
        bot,
        chat_id: int,
        policy_resolver: PolicyResolver | None = None,
        action_recorder: ActionRecorder | None = None,
    ):
        self.bot = bot
        self.chat_id = chat_id
        self.policy_resolver = policy_resolver
        self.action_recorder = action_recorder
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
        if any(
            action.expires_at is not None and action.expires_at <= now for action in intent.actions
        ):
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
        rendered = self._renderer.render(intent, now=now)
        if rendered.actions and self.action_recorder is not None:
            self.action_recorder(intent, rendered.actions, now)
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
            if provider_reference is not None:
                return DeliveryAttempt(
                    intent_id=intent.intent_id,
                    state=DeliveryState.UNCERTAIN,
                    rendered=rendered,
                    receipt=DeliveryReceipt(
                        intent_id=intent.intent_id,
                        state=DeliveryState.UNCERTAIN,
                        observed_at=now,
                        provider_reference=provider_reference,
                        detail="Telegram accepted only part of a multi-message intent",
                    ),
                    reason="Telegram rate limit after a partial send",
                )
            return DeliveryAttempt(
                intent_id=intent.intent_id,
                state=DeliveryState.UNCERTAIN if provider_reference else DeliveryState.QUEUED,
                rendered=rendered,
                reason=(
                    "Telegram rate limit after partial delivery"
                    if provider_reference
                    else "Telegram rate limit"
                ),
                retry_after=now + timedelta(seconds=seconds) if not provider_reference else None,
            )
        except BadRequest:
            if provider_reference is not None:
                return DeliveryAttempt(
                    intent_id=intent.intent_id,
                    state=DeliveryState.UNCERTAIN,
                    rendered=rendered,
                    receipt=DeliveryReceipt(
                        intent_id=intent.intent_id,
                        state=DeliveryState.UNCERTAIN,
                        observed_at=now,
                        provider_reference=provider_reference,
                        detail="Telegram accepted only part of a multi-message intent",
                    ),
                    reason="Telegram rejected a later part of the intent",
                )
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
                receipt=DeliveryReceipt(
                    intent_id=intent.intent_id,
                    state=DeliveryState.UNCERTAIN,
                    observed_at=now,
                    provider_reference=provider_reference,
                    detail="Telegram send outcome is unknown",
                ),
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
