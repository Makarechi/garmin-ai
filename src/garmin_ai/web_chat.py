"""Authenticated same-origin web chat using the neutral inbox and outbox."""

import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner
from garmin_ai.channels import (
    ChannelInstanceRef,
    DeliveryReceipt,
    DeliveryState,
    InboundEnvelope,
    InboundKind,
    OutboundIntent,
    TextBlock,
)
from garmin_ai.db import transaction
from garmin_ai.dialogue import DialogueService, record_delivery_receipt
from garmin_ai.events import Conflict, EventInput, StrictModel, create_event, lock_writes
from garmin_ai.models import AppState, Event, EventDefinitionVersion, InboundMessage, OutboxMessage
from garmin_ai.normalize import upsert

CHANNEL = ChannelInstanceRef(channel="web", instance_id="local")
DESTINATION = "web:local"
MAX_MESSAGES = 100


class WebChatSend(StrictModel):
    client_message_id: UUID
    text: str = Field(min_length=1, max_length=16000)


def conversation_id(owner_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"garmin-ai/web-chat/{owner_id}")


def _reply(session, actor, text: str) -> OutboundIntent:
    requirements = session.info.get("channel_share_requirements", {})
    evidence_refs = []
    for version_id in requirements:
        version = session.get(EventDefinitionVersion, UUID(version_id))
        if version is not None:
            evidence_refs.append(f"definition:{version.definition_id}")
    return OutboundIntent(
        owner_id=actor.owner_id,
        conversation_id=actor.conversation_id,
        channel_instance=CHANNEL,
        blocks=[TextBlock(text=text)],
        evidence_refs=sorted(set(evidence_refs)),
        initiative=False,
    )


def _set_paused(session, enabled: bool, now: datetime) -> str:
    from garmin_ai.initiative_rules import cancel_queued_initiatives
    from garmin_ai.models import PendingQuestion

    lock_writes(session)
    session.execute(select(func.pg_advisory_xact_lock(72104621)))
    upsert(
        session,
        AppState,
        {
            "key": "proactive:enabled",
            "value": {
                "enabled": enabled,
                "message_at": int(now.timestamp()),
                "ordering_epoch": 0,
                "update_id": 0,
            },
        },
        ["key"],
    )
    if not enabled:
        cancel_queued_initiatives(session)
        for question in session.scalars(
            select(PendingQuestion).where(
                or_(
                    (PendingQuestion.status == "pending") & PendingQuestion.sent_at.is_(None),
                    PendingQuestion.status == "sending",
                )
            )
        ):
            question.status = "cancelled"
            question.evidence = {**question.evidence, "cancel_reason": "owner_pause"}
    return "Инициативные сообщения включены." if enabled else "Инициативные сообщения остановлены."


def _history(session, timezone: str) -> str:
    from garmin_ai.share_policy import event_sharing_filter, track_channel_share

    rows = session.scalars(
        select(Event)
        .where(
            Event.deleted.is_(False),
            event_sharing_filter(
                destination_kind="channel",
                destination_instance_id=DESTINATION,
                categories={"schema", "facts"},
            ),
        )
        .order_by(Event.start.desc(), Event.id.desc())
        .limit(5)
    ).all()
    if not rows:
        return "Пока нет доступных записей. Откройте дневник для подробностей."
    for row in rows:
        if row.kind.startswith("user.") and row.definition_version_id:
            track_channel_share(session, row.definition_version_id, {"schema", "facts"})
    return (
        "Последние записи: "
        + "; ".join(
            f"{row.kind} ({row.start.astimezone(ZoneInfo(timezone)).strftime('%d.%m %H:%M')})"
            for row in rows
        )
        + ". Подробности и исправления доступны в дневнике."
    )


def _process_text(session, actor, envelope, settings, engine) -> OutboundIntent:
    from garmin_ai.agent import apply_command, interpret
    from garmin_ai.diary_forms import obvious_urgent_symptoms, urgent_notice
    from garmin_ai.integrations import (
        IntegrationUnavailable,
        configured_model_instance,
        create_model_provider,
    )
    from garmin_ai.llm import ProviderOutputInvalid, ProviderRequestInvalid, ProviderUnavailable
    from garmin_ai.onboarding import model_category_selected

    now = datetime.now(UTC)
    text = (envelope.text or "").strip()
    session.info["channel_instance"] = CHANNEL
    session.info["channel_destination_instance_id"] = DESTINATION
    session.info["conversation_now"] = now
    session.info["locale"] = settings.locale
    session.info["timezone"] = settings.timezone
    model_instance = configured_model_instance(settings)
    session.info["model_provider_instance_id"] = (
        model_instance.id if model_instance is not None else "model:gemini:primary"
    )
    if obvious_urgent_symptoms(text):
        response = urgent_notice(settings.locale)
    elif text == "/help":
        response = "Команды: /note текст, /history, /pause, /resume, /cancel. Формы и дневник доступны на /dashboard."
    elif text == "/history":
        response = _history(session, settings.timezone)
    elif text in {"/pause", "/resume"}:
        response = _set_paused(session, text == "/resume", now)
    elif text == "/cancel":
        for key in ("conversation:pending:web:local", "analysis:conversation:pending:web:local"):
            pending = session.get(AppState, key)
            if pending is not None:
                session.delete(pending)
        response = "Текущее уточнение отменено."
    elif text.startswith("/note "):
        content = text.removeprefix("/note ").strip()
        event = EventInput(
            start=now,
            timezone=settings.timezone,
            source="manual",
            original_text=content,
            payload={"type": "note", "description": content},
        )
        create_event(
            session,
            event,
            actor=DESTINATION,
            idempotency_key=f"web-chat:{envelope.external_event_id}",
            operation_id=actor.operation_id,
        )
        response = "Заметка сохранена. Откройте дневник, чтобы просмотреть или исправить её."
    else:
        provider = None
        if model_instance is not None and model_category_selected(session, "diary"):
            try:
                provider = create_model_provider(settings, engine)
            except (IntegrationUnavailable, ProviderUnavailable, ValueError):
                pass
        if provider is None:
            response = (
                "Модель недоступна. Для записи используйте /note текст или формы на /dashboard."
            )
        else:
            try:
                command = interpret(session, provider, text, settings, now, source="manual")
                if command.intent == "safety":
                    response = urgent_notice(settings.locale)
                elif command.intent == "question":
                    from garmin_ai.agent import answer_question

                    response = answer_question(
                        session,
                        provider,
                        text,
                        settings,
                        now,
                        update_id=envelope.external_event_id,
                    )
                else:
                    response = apply_command(
                        session,
                        command,
                        text=text,
                        update_id=envelope.external_event_id,
                        actor=DESTINATION,
                        now=now,
                        idempotency_prefix=f"web-chat:{envelope.external_event_id}",
                        operation_id=actor.operation_id,
                    )
            except (ProviderUnavailable, ProviderOutputInvalid, ProviderRequestInvalid):
                response = "Модель не смогла надёжно обработать сообщение. Ничего не сохранено; попробуйте форму в дневнике."
            finally:
                provider.close()
    return _reply(session, actor, response)


def _outbox_payload(row: OutboxMessage) -> dict:
    intent = OutboundIntent.model_validate(row.intent)
    return {
        "id": str(row.id),
        "inbound_message_id": str(row.inbound_message_id) if row.inbound_message_id else None,
        "text": "\n".join(block.text for block in intent.blocks),
        "state": row.state,
        "created_at": row.created_at.isoformat(),
    }


def _intent_allowed(session, value: dict) -> bool:
    from garmin_ai.share_policy import sharing_allowed

    intent = OutboundIntent.model_validate(value)
    return all(
        sharing_allowed(
            session,
            UUID(reference.removeprefix("definition:")),
            destination_kind="channel",
            destination_instance_id=DESTINATION,
            categories={"schema", "facts"},
        )
        for reference in intent.evidence_refs
        if reference.startswith("definition:")
    )


def _channel_allowed(session, row: OutboxMessage) -> bool:
    return _intent_allowed(session, row.intent)


def _fenced_response(engine, body: dict, intents: dict[str, dict]):
    """Recheck consent while the HTTP body is actually sent to the browser."""

    def content():
        from garmin_ai.share_policy import channel_consent_delivery_fence

        # Match consent writers' replay-lock -> consent-lock order.
        with transaction(engine) as fresh:
            with channel_consent_delivery_fence(engine):
                allowed = {
                    identity
                    for identity, intent in intents.items()
                    if _intent_allowed(fresh, intent)
                }
                if "reply" in body and body["reply"] is not None:
                    body["reply"] = body["reply"] if body["reply"]["id"] in allowed else None
                if "replies" in body:
                    body["replies"] = [item for item in body["replies"] if item["id"] in allowed]
                yield json.dumps(body, ensure_ascii=False).encode("utf-8")

    return StreamingResponse(content(), media_type="application/json")


def install_web_chat(app, settings, engine, db, authorize):
    from garmin_ai.access import permits

    def require_owner(granted: Annotated[frozenset[str], Depends(authorize)]):
        if not permits(granted, {"admin"}):
            raise HTTPException(403, "Owner token required")

    @app.post("/web-chat/messages", dependencies=[Depends(require_owner)])
    def send(body: WebChatSend, session: Annotated[Session, Depends(db)]):
        person = owner(session)
        envelope = InboundEnvelope(
            owner_id=person.id,
            channel_instance=CHANNEL,
            conversation_id=conversation_id(person.id),
            external_event_id=str(body.client_message_id),
            external_message_id=str(body.client_message_id),
            sender_ref="owner",
            received_at=datetime.now(UTC),
            kind=InboundKind.TEXT,
            text=body.text,
        )
        result = DialogueService().process(
            session,
            envelope,
            lambda active, actor, item: _process_text(active, actor, item, settings, engine),
        )
        inbound = session.get(InboundMessage, result.inbound_message_id)
        if result.duplicate and inbound is not None and inbound.normalized_text != body.text:
            raise Conflict("Message ID was already used for different text")
        outbox = (
            session.get(OutboxMessage, result.outbox_message_id)
            if result.outbox_message_id is not None
            else None
        )
        body = {
            "duplicate": result.duplicate,
            "message_id": str(result.inbound_message_id),
            "reply": _outbox_payload(outbox) if outbox is not None else None,
        }
        intents = {str(outbox.id): outbox.intent} if outbox is not None else {}
        return _fenced_response(engine, body, intents)

    @app.get("/web-chat/messages", dependencies=[Depends(require_owner)])
    def messages(session: Annotated[Session, Depends(db)]):
        person = owner(session)
        conversation = conversation_id(person.id)
        inbound = session.scalars(
            select(InboundMessage)
            .where(
                InboundMessage.owner_id == person.id,
                InboundMessage.conversation_id == conversation,
                InboundMessage.channel == CHANNEL.channel,
                InboundMessage.channel_instance_id == CHANNEL.instance_id,
            )
            .order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc())
            .limit(MAX_MESSAGES)
        ).all()
        outbox = session.scalars(
            select(OutboxMessage)
            .where(
                OutboxMessage.owner_id == person.id,
                OutboxMessage.conversation_id == conversation,
                OutboxMessage.state.in_(["queued", "read"]),
            )
            .order_by(OutboxMessage.created_at.desc(), OutboxMessage.id.desc())
            .limit(MAX_MESSAGES)
        ).all()
        body = {
            "messages": [
                {
                    "id": str(row.id),
                    "text": row.normalized_text,
                    "created_at": row.received_at.isoformat(),
                }
                for row in reversed(inbound)
            ],
            "replies": [_outbox_payload(row) for row in reversed(outbox)],
        }
        return _fenced_response(engine, body, {str(row.id): row.intent for row in outbox})

    @app.post("/web-chat/messages/{outbox_id}/read", dependencies=[Depends(require_owner)])
    def mark_read(outbox_id: UUID, session: Annotated[Session, Depends(db)]):
        person = owner(session)
        row = session.get(OutboxMessage, outbox_id)
        if (
            row is None
            or row.owner_id != person.id
            or row.conversation_id != conversation_id(person.id)
            or row.inbound_message_id is None
        ):
            raise HTTPException(404, "Message not found")
        from garmin_ai.share_policy import channel_consent_delivery_fence

        lock_writes(session)
        with channel_consent_delivery_fence(engine):
            if not _channel_allowed(session, row):
                raise HTTPException(409, "Message is no longer available")
            if row.state == DeliveryState.QUEUED.value:
                record_delivery_receipt(
                    session,
                    row.id,
                    DeliveryReceipt(
                        intent_id=row.id, state=DeliveryState.READ, observed_at=datetime.now(UTC)
                    ),
                )
            elif row.state != DeliveryState.READ.value:
                raise HTTPException(409, "Message is no longer available")
            inbound = session.get(InboundMessage, row.inbound_message_id)
            if inbound is not None:
                upsert(
                    session,
                    AppState,
                    {
                        "key": f"outbox:web:local:update:{inbound.external_event_id}:0",
                        "value": {"status": "sent"},
                    },
                    ["key"],
                )
                from garmin_ai.conversation import promote_delivered

                session.info["channel_destination_instance_id"] = DESTINATION
                promote_delivered(session, datetime.now(UTC))
        return {"status": "read"}
