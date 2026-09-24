from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner
from garmin_ai.channels import (
    ActionRef,
    AttachmentRef,
    DeliveryState,
    InboundEnvelope,
    InboundKind,
    OutboundIntent,
    TextBlock,
)
from garmin_ai.dialogue import DialogueService, record_delivery_receipt
from garmin_ai.models import AppState, OutboxMessage
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


@pytest.mark.anyio
async def test_restricted_channel_does_not_acknowledge_dropped_attachments():
    channel = RestrictedTextChannel()
    attempt = await channel.deliver(
        OutboundIntent(
            owner_id=uuid4(),
            conversation_id=uuid4(),
            channel_instance=RESTRICTED_INSTANCE,
            blocks=[TextBlock(text="synthetic attachment")],
            attachments=[AttachmentRef(kind="file", external_id="opaque-file")],
        ),
        now=NOW,
    )

    assert attempt.state is DeliveryState.QUEUED
    assert "not implemented" in attempt.reason


@pytest.mark.anyio
async def test_restricted_channel_returns_expired_attempt_for_stale_action(db_engine):
    channel = RestrictedTextChannel(session_factory=lambda: Session(db_engine))
    intent = OutboundIntent(
        owner_id=uuid4(),
        conversation_id=uuid4(),
        channel_instance=RESTRICTED_INSTANCE,
        blocks=[TextBlock(text="Choose")],
        actions=[
            ActionRef(
                action_id="expired",
                label="Expired",
                operation_id=uuid4(),
                expires_at=NOW,
            )
        ],
    )

    attempt = await channel.deliver(intent, now=NOW)

    assert attempt.state is DeliveryState.EXPIRED
    assert attempt.intent_id == intent.intent_id
    with Session(db_engine) as session:
        assert (
            session.query(AppState).filter(AppState.key.startswith("restricted-action:")).count()
            == 0
        )


@pytest.mark.anyio
async def test_reference_receipt_mapping_expires_without_confirmation(db, db_engine):
    channel = RestrictedTextChannel(session_factory=lambda: Session(db_engine))

    async def send(at):
        return await channel.deliver(
            OutboundIntent(
                owner_id=uuid4(),
                conversation_id=uuid4(),
                channel_instance=RESTRICTED_INSTANCE,
                blocks=[TextBlock(text="Synthetic receipt")],
            ),
            now=at,
        )

    old = await send(NOW)
    key = channel._storage_key("receipt", old.receipt.provider_reference)
    assert db.get(AppState, key) is not None
    await send(NOW + timedelta(days=8))
    db.expire_all()
    assert db.get(AppState, key) is None
    assert (
        channel.confirm_delivery(
            old.receipt.provider_reference, now=NOW + timedelta(days=8), session=db
        )
        is None
    )


def test_reference_retry_rechecks_ingress_after_consumption_race(db, db_engine, monkeypatch):
    channel = RestrictedTextChannel(session_factory=lambda: Session(db_engine))
    owner_id, conversation_id = owner(db).id, uuid4()
    db.commit()
    token = "synthetic-consumed-token"
    action = ActionRef(action_id="confirm", label="Confirm", operation_id=uuid4(), token=token)
    stored = InboundEnvelope(
        owner_id=owner_id,
        channel_instance=RESTRICTED_INSTANCE,
        conversation_id=conversation_id,
        external_event_id="opaque:race",
        sender_ref="synthetic-sender",
        received_at=NOW,
        kind=InboundKind.ACTION,
        action=action,
    )

    def consumed_during_wait(*_args, **_kwargs):
        with Session(db_engine) as session:
            DialogueService().process(session, stored, lambda *_args: None)
            session.commit()
        return None

    monkeypatch.setattr(channel, "consume_action", consumed_during_wait)
    replay = channel.receive_action_token(
        owner_id=owner_id,
        conversation_id=conversation_id,
        external_event_id="opaque:race",
        sender_ref="synthetic-sender",
        token=token,
        received_at=NOW,
        session=db,
    )

    assert replay == stored


@pytest.mark.anyio
async def test_reference_action_and_receipt_survive_adapter_restart(db, db_engine):
    def factory():
        return Session(db_engine)

    channel = RestrictedTextChannel(session_factory=factory)
    owner_id, conversation_id = uuid4(), uuid4()
    intent = OutboundIntent(
        owner_id=owner_id,
        conversation_id=conversation_id,
        channel_instance=RESTRICTED_INSTANCE,
        blocks=[TextBlock(text="Choose")],
        actions=[
            ActionRef(
                action_id="confirm:v2",
                label="Confirm",
                operation_id=uuid4(),
                expires_at=NOW + timedelta(minutes=5),
            )
        ],
    )
    attempt = await channel.deliver(intent, now=NOW)
    assert attempt.state is DeliveryState.PROVIDER_ACCEPTED
    token = attempt.rendered.texts[-1].split("[", 1)[1].removesuffix("]")

    restarted = RestrictedTextChannel(session_factory=factory)
    assert (
        restarted.consume_action(token, owner_id=uuid4(), conversation_id=conversation_id, now=NOW)
        is None
    )
    selected = restarted.consume_action(
        token, owner_id=owner_id, conversation_id=conversation_id, now=NOW
    )
    assert selected.action_id == "confirm:v2"
    assert (
        restarted.consume_action(token, owner_id=owner_id, conversation_id=conversation_id, now=NOW)
        is None
    )
    with factory() as session:
        receipt = restarted.confirm_delivery(
            attempt.receipt.provider_reference, now=NOW, session=session
        )
        session.commit()
    assert receipt.intent_id == intent.intent_id and receipt.confirms_delivery
    with factory() as session:
        assert (
            restarted.confirm_delivery(attempt.receipt.provider_reference, now=NOW, session=session)
            is None
        )


@pytest.mark.anyio
async def test_reference_receipt_survives_rollback_before_neutral_record(db, db_engine):
    def factory():
        return Session(db_engine)

    channel = RestrictedTextChannel(session_factory=factory)
    intent = OutboundIntent(
        owner_id=uuid4(),
        conversation_id=uuid4(),
        channel_instance=RESTRICTED_INSTANCE,
        blocks=[TextBlock(text="Synthetic receipt")],
    )
    attempt = await channel.deliver(intent, now=NOW)
    with factory() as session:
        receipt = channel.confirm_delivery(
            attempt.receipt.provider_reference, now=NOW, session=session
        )
        assert receipt.intent_id == intent.intent_id
        session.rollback()

    with factory() as session:
        receipt = RestrictedTextChannel(session_factory=factory).confirm_delivery(
            attempt.receipt.provider_reference, now=NOW, session=session
        )
        assert receipt.intent_id == intent.intent_id
        session.commit()


@pytest.mark.anyio
async def test_reference_delivery_reaps_expired_action_tokens(db, db_engine):
    def factory():
        return Session(db_engine)

    channel = RestrictedTextChannel(session_factory=factory)
    owner_id, conversation_id = uuid4(), uuid4()

    async def send(at, label):
        intent = OutboundIntent(
            owner_id=owner_id,
            conversation_id=conversation_id,
            channel_instance=RESTRICTED_INSTANCE,
            blocks=[TextBlock(text=label)],
            actions=[ActionRef(action_id=label, label=label, operation_id=uuid4())],
        )
        attempt = await channel.deliver(intent, now=at)
        return attempt.rendered.texts[-1].split("[", 1)[1].removesuffix("]")

    old_token = await send(NOW, "Old")
    assert db.get(AppState, channel._storage_key("action", old_token)) is not None
    await send(NOW + timedelta(minutes=16), "New")
    db.expire_all()
    assert db.get(AppState, channel._storage_key("action", old_token)) is None


@pytest.mark.anyio
async def test_reference_text_action_and_receipt_use_real_neutral_ingress(db, db_engine):
    def factory():
        return Session(db_engine)

    channel = RestrictedTextChannel(session_factory=factory)
    now = datetime.now(UTC)
    owner_id, conversation_id = owner(db).id, uuid4()
    initial = channel.receive_text(
        owner_id=owner_id,
        conversation_id=conversation_id,
        external_event_id="opaque:first",
        sender_ref="synthetic-sender",
        text="choose",
        received_at=now,
    )
    calls = []

    def handler(_session, actor, incoming):
        calls.append((actor.operation_id, incoming.kind))
        return OutboundIntent(
            owner_id=actor.owner_id,
            conversation_id=actor.conversation_id,
            channel_instance=RESTRICTED_INSTANCE,
            blocks=[TextBlock(text="Choose" if incoming.kind is InboundKind.TEXT else "Recorded")],
            actions=(
                [
                    ActionRef(
                        action_id="confirm:v2",
                        label="Confirm",
                        operation_id=uuid4(),
                        expires_at=now + timedelta(minutes=5),
                    )
                ]
                if incoming.kind is InboundKind.TEXT
                else []
            ),
        )

    service = DialogueService()
    first = service.process(db, initial, handler)
    db.commit()
    queued = db.get(OutboxMessage, first.outbox_message_id)
    accepted = await channel.deliver(OutboundIntent.model_validate(queued.intent), now=now)
    record_delivery_receipt(db, queued.id, accepted.receipt)
    assert queued.state == DeliveryState.PROVIDER_ACCEPTED.value
    delivered = channel.confirm_delivery(accepted.receipt.provider_reference, now=now, session=db)
    record_delivery_receipt(db, queued.id, delivered)
    assert queued.state == DeliveryState.DELIVERED.value
    token = accepted.rendered.texts[-1].split("[", 1)[1].removesuffix("]")
    db.commit()

    restarted = RestrictedTextChannel(session_factory=factory)
    action = restarted.receive_action_token(
        owner_id=owner_id,
        conversation_id=conversation_id,
        external_event_id="opaque:second",
        sender_ref="synthetic-sender",
        token=token,
        received_at=now,
        session=db,
    )
    assert action.kind is InboundKind.ACTION and action.action.action_id == "confirm:v2"
    second = service.process(db, action, handler)
    db.commit()  # Replays must work after the token deletion is durable.
    retries = [
        service.process(
            db,
            restarted.receive_action_token(
                owner_id=owner_id,
                conversation_id=conversation_id,
                external_event_id="opaque:second",
                sender_ref="synthetic-sender",
                token=token,
                received_at=now,
                session=db,
            ),
            handler,
        )
        for _ in range(10)
    ]
    db.commit()

    assert all(result.duplicate for result in retries)
    assert len(calls) == 2
    assert db.query(OutboxMessage).count() == 2
    assert second.outbox_message_id is not None
    with pytest.raises(PermissionError):
        restarted.receive_action_token(
            owner_id=owner_id,
            conversation_id=conversation_id,
            external_event_id="opaque:second",
            sender_ref="synthetic-sender",
            token="different-token-value",
            received_at=now,
            session=db,
        )
    with pytest.raises(LookupError):
        restarted.receive_action_token(
            owner_id=owner_id,
            conversation_id=conversation_id,
            external_event_id="opaque:third",
            sender_ref="synthetic-sender",
            token=token,
            received_at=now,
            session=db,
        )
