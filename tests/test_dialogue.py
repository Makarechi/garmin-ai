from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from garmin_ai.accounts import owner
from garmin_ai.channels import (
    ActionRef,
    ChannelInstanceRef,
    DeliveryReceipt,
    DeliveryState,
    InboundEnvelope,
    InboundKind,
    OutboundIntent,
    TextBlock,
)
from garmin_ai.dialogue import (
    CommandDispatcher,
    CommandRequest,
    DialogueService,
    actor_context,
    claim_outbox,
    explicitly_requeue_uncertain,
    ingest_envelope,
    record_delivery_receipt,
    recover_expired_outbox_leases,
)
from garmin_ai.events import Conflict
from garmin_ai.models import Conversation, InboundMessage, MessageDeliveryReceipt, OutboxMessage

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def envelope(person, conversation_id=None, **updates):
    values = {
        "owner_id": person.id,
        "channel_instance": ChannelInstanceRef(channel="test", instance_id="restricted"),
        "conversation_id": conversation_id or uuid4(),
        "external_event_id": "event-123",
        "external_message_id": "message-123",
        "sender_ref": "owner-123",
        "occurred_at": NOW,
        "received_at": NOW,
        "kind": InboundKind.TEXT,
        "text": "hello",
    }
    values.update(updates)
    return InboundEnvelope(**values)


def response(source: InboundEnvelope, text="done"):
    return OutboundIntent(
        owner_id=source.owner_id,
        conversation_id=source.conversation_id,
        channel_instance=source.channel_instance,
        blocks=[TextBlock(text=text)],
    )


def test_ten_transport_retries_run_one_operation_and_create_one_outbox(db):
    person = owner(db)
    source = envelope(person)
    service = DialogueService()
    calls = []

    def handler(_session, actor, incoming):
        calls.append(actor.operation_id)
        return response(incoming)

    results = []
    for _ in range(10):
        retried = source.model_copy(update={"message_id": uuid4()})
        results.append(service.process(db, retried, handler))

    assert len(calls) == 1
    assert not results[0].duplicate
    assert all(item.duplicate for item in results[1:])
    assert db.scalar(select(func.count()).select_from(InboundMessage)) == 1
    assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 1


def test_conversation_pending_state_and_forget_epoch_are_isolated(db):
    person = owner(db)
    service = DialogueService()
    first, _ = ingest_envelope(db, envelope(person, external_event_id="one"))
    second, _ = ingest_envelope(db, envelope(person, external_event_id="two"))
    service.set_pending(db, first.conversation_id, {"question": "first"})
    service.set_pending(db, second.conversation_id, {"question": "second"})
    stale_epoch = service.begin_generation(db, first.conversation_id)

    service.forget(db, first.conversation_id)

    assert service.pending(db, first.conversation_id) is None
    assert service.pending(db, second.conversation_id) == {"question": "second"}
    assert (
        service.queue_generation_result(
            db,
            response(envelope(person, conversation_id=first.conversation_id)),
            expected_epoch=stale_epoch,
            operation_id=uuid4(),
        )
        is None
    )


def test_generated_intent_must_match_authenticated_channel(db):
    person = owner(db)
    service = DialogueService()
    source = envelope(person)
    row, _ = ingest_envelope(db, source)
    intent = response(source).model_copy(
        update={"channel_instance": ChannelInstanceRef(channel="test", instance_id="other")}
    )

    with pytest.raises(PermissionError, match="authenticated conversation"):
        service.queue_generation_result(
            db,
            intent,
            expected_epoch=service.begin_generation(db, row.conversation_id),
            operation_id=uuid4(),
        )
    assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 0


def test_edit_is_a_revision_of_the_same_operation_and_stale_edit_conflicts(db):
    person = owner(db)
    conversation_id = uuid4()
    original, created = ingest_envelope(db, envelope(person, conversation_id=conversation_id))
    edited, created_edit = ingest_envelope(
        db,
        envelope(
            person,
            conversation_id=conversation_id,
            message_id=uuid4(),
            external_event_id="edit-123",
            kind=InboundKind.EDIT,
            text="corrected",
            revision=2,
        ),
    )

    assert created and created_edit
    assert edited.supersedes_id == original.id
    assert edited.operation_id == original.operation_id
    with pytest.raises(Conflict, match="not newer"):
        ingest_envelope(
            db,
            envelope(
                person,
                conversation_id=conversation_id,
                message_id=uuid4(),
                external_event_id="another-edit",
                kind=InboundKind.EDIT,
                text="stale",
                revision=1,
            ),
        )


def test_edit_response_has_revision_dedup_without_reusing_original_outbox(db):
    person = owner(db)
    conversation_id = uuid4()
    service = DialogueService()
    original = envelope(person, conversation_id=conversation_id)
    first = service.process(db, original, lambda *_args: response(original, "original"))
    edit = envelope(
        person,
        conversation_id=conversation_id,
        message_id=uuid4(),
        external_event_id="edit-123",
        kind=InboundKind.EDIT,
        text="corrected",
        revision=2,
    )
    second = service.process(db, edit, lambda *_args: response(edit, "corrected"))

    assert first.operation_id == second.operation_id
    assert first.outbox_message_id != second.outbox_message_id
    assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 2


def test_distinct_actions_on_one_message_use_distinct_operations(db):
    person = owner(db)
    conversation_id = uuid4()
    service = DialogueService()
    results = []
    for index in range(2):
        incoming = envelope(
            person,
            conversation_id=conversation_id,
            message_id=uuid4(),
            external_event_id=f"action-{index}",
            kind=InboundKind.ACTION,
            action=ActionRef(
                action_id=f"choice-{index}",
                label=f"Choice {index}",
                operation_id=uuid4(),
            ),
        )
        results.append(
            service.process(db, incoming, lambda *_args, incoming=incoming: response(incoming))
        )

    assert results[0].operation_id != results[1].operation_id
    assert results[0].outbox_message_id != results[1].outbox_message_id


def test_receipt_evidence_never_regresses_read_to_provider_acceptance(db):
    person = owner(db)
    source = envelope(person)
    result = DialogueService().process(db, source, lambda *_args: response(source))
    assert result.outbox_message_id is not None
    for state in (DeliveryState.READ, DeliveryState.PROVIDER_ACCEPTED):
        record_delivery_receipt(
            db,
            result.outbox_message_id,
            DeliveryReceipt(
                intent_id=result.outbox_message_id,
                state=state,
                observed_at=NOW,
                provider_reference="opaque-provider-ref",
            ),
        )

    assert db.get(OutboxMessage, result.outbox_message_id).state == DeliveryState.READ.value


def test_repeated_delivery_receipt_is_idempotent(db):
    person = owner(db)
    source = envelope(person)
    result = DialogueService().process(db, source, lambda *_args: response(source))
    receipt = DeliveryReceipt(
        intent_id=result.outbox_message_id,
        state=DeliveryState.DELIVERED,
        observed_at=NOW,
        provider_reference="opaque-provider-ref",
    )

    first = record_delivery_receipt(db, result.outbox_message_id, receipt)
    replay = record_delivery_receipt(db, result.outbox_message_id, receipt)

    assert replay.id == first.id
    assert db.scalar(select(func.count()).select_from(MessageDeliveryReceipt)) == 1


def test_abandoned_send_becomes_uncertain_and_requires_explicit_requeue(db):
    person = owner(db)
    source = envelope(person)
    result = DialogueService().process(db, source, lambda *_args: response(source))
    lease = claim_outbox(db, NOW)

    assert lease.outbox_message_id == result.outbox_message_id
    assert recover_expired_outbox_leases(db, NOW) == 0
    assert recover_expired_outbox_leases(db, NOW.replace(hour=13)) == 1
    row = db.get(OutboxMessage, result.outbox_message_id)
    assert row.state == DeliveryState.UNCERTAIN.value
    assert claim_outbox(db, NOW.replace(hour=14)) is None
    with pytest.raises(PermissionError):
        explicitly_requeue_uncertain(db, row.id)
    explicitly_requeue_uncertain(db, row.id, authorized=True)
    assert claim_outbox(db, NOW.replace(hour=14)).outbox_message_id == row.id


def test_dispatcher_uses_neutral_actor_permissions_and_semantic_names(db):
    person = owner(db)
    source = envelope(person)
    row, _ = ingest_envelope(db, source)
    dispatcher = CommandDispatcher()
    dispatcher.register(
        "goals.update",
        lambda _session, _actor, arguments: arguments["value"],
        permissions=frozenset({"goals:write"}),
    )

    with pytest.raises(PermissionError):
        dispatcher.dispatch(db, actor_context(row), CommandRequest(name="goals.update"))
    permitted = actor_context(row, frozenset({"goals:write"}))
    assert (
        dispatcher.dispatch(
            db,
            permitted,
            CommandRequest(name="goals.update", arguments={"value": "saved"}),
        )
        == "saved"
    )


def test_conversation_identity_cannot_be_reused_by_another_channel(db):
    person = owner(db)
    conversation_id = uuid4()
    ingest_envelope(db, envelope(person, conversation_id=conversation_id))

    with pytest.raises(PermissionError, match="another channel"):
        ingest_envelope(
            db,
            envelope(
                person,
                conversation_id=conversation_id,
                external_event_id="other",
                channel_instance=ChannelInstanceRef(channel="other", instance_id="restricted"),
            ),
        )

    assert db.scalar(select(func.count()).select_from(Conversation)) == 1
