import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

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
    prune_neutral_analysis,
    queue_intent,
    record_delivery_receipt,
    recover_expired_outbox_leases,
)
from garmin_ai.events import Conflict
from garmin_ai.models import Conversation, InboundMessage, MessageDeliveryReceipt, OutboxMessage

NOW = (datetime.now(UTC) - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)


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


def test_committed_pending_ingress_resumes_once_from_stored_envelope(db):
    source = envelope(owner(db))
    row, created = ingest_envelope(db, source)
    assert created
    operation_id = row.operation_id
    db.commit()
    calls = []
    retry = source.model_copy(update={"message_id": uuid4(), "text": "changed retry payload"})

    def handler(_session, actor, incoming):
        calls.append((actor.operation_id, incoming.text))
        return response(incoming)

    service = DialogueService()
    resumed = service.process(db, retry, handler)
    repeated = service.process(db, retry, handler)

    assert resumed.duplicate and resumed.status == "processed"
    assert repeated.duplicate and repeated.outbox_message_id == resumed.outbox_message_id
    assert calls == [(operation_id, "hello")]
    assert db.scalar(select(func.count()).select_from(InboundMessage)) == 1
    assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 1


def test_pending_ingress_with_committed_outbox_does_not_repeat_operation(db):
    source = envelope(owner(db))
    row, _ = ingest_envelope(db, source)
    queued = queue_intent(
        db,
        response(source),
        operation_id=row.operation_id,
        inbound_message_id=row.id,
    )
    db.commit()

    def handler(*_args):
        raise AssertionError("Operation must not run again")

    result = DialogueService().process(db, source, handler)

    assert result.duplicate and result.status == "processed"
    assert result.outbox_message_id == queued.id


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
            inbound_message_id=uuid4(),
        )
        is None
    )


def test_confirmed_analysis_memory_is_isolated_and_explicitly_shared(db):
    person = owner(db)
    service = DialogueService()
    first, _ = ingest_envelope(db, envelope(person, external_event_id="analysis-one"))
    second, _ = ingest_envelope(db, envelope(person, external_event_id="analysis-two"))
    first_source = envelope(person, conversation_id=first.conversation_id)
    outbox = queue_intent(
        db,
        response(first_source),
        operation_id=first.operation_id,
        inbound_message_id=first.id,
    )
    epoch = service.begin_generation(db, first.conversation_id)

    with pytest.raises(Conflict, match="confirmed"):
        service.remember_analysis(
            db,
            first.conversation_id,
            operation_id=first.operation_id,
            outbox_id=outbox.id,
            expected_epoch=epoch,
            question="hello",
            answer="done",
        )
    outbox.state = DeliveryState.DELIVERED.value
    assert service.remember_analysis(
        db,
        first.conversation_id,
        operation_id=first.operation_id,
        outbox_id=outbox.id,
        expected_epoch=epoch,
        question="hello",
        answer="done",
    )
    assert not service.remember_analysis(
        db,
        first.conversation_id,
        operation_id=first.operation_id,
        outbox_id=outbox.id,
        expected_epoch=epoch,
        question="hello",
        answer="done",
    )
    assert len(service.analysis_context(db, first.conversation_id, NOW)) == 1
    assert service.analysis_context(db, second.conversation_id, NOW) == []

    service.set_owner_memory_sharing(db, second.conversation_id, True)
    assert service.analysis_context(db, second.conversation_id, NOW) == []
    service.set_owner_memory_sharing(db, first.conversation_id, True)
    assert service.analysis_context(db, second.conversation_id, NOW)[0]["question"] == "hello"
    service.forget(db, first.conversation_id)
    assert service.analysis_context(db, second.conversation_id, NOW) == []
    assert not service.remember_analysis(
        db,
        first.conversation_id,
        operation_id=first.operation_id,
        outbox_id=outbox.id,
        expected_epoch=epoch,
        question="hello",
        answer="done",
    )


def test_analysis_memory_survives_restart_then_prunes_after_seven_days(db):
    source = envelope(owner(db))
    row, _ = ingest_envelope(db, source)
    outbox = queue_intent(
        db,
        response(source),
        operation_id=row.operation_id,
        inbound_message_id=row.id,
    )
    outbox.state = DeliveryState.DELIVERED.value
    service = DialogueService()
    assert service.remember_analysis(
        db,
        row.conversation_id,
        operation_id=row.operation_id,
        outbox_id=outbox.id,
        expected_epoch=service.begin_generation(db, row.conversation_id),
        question="hello",
        answer="done",
    )
    conversation_id = row.conversation_id
    db.commit()
    db.expunge_all()

    assert service.analysis_context(db, conversation_id, NOW)[0]["answer"] == "done"
    assert service.analysis_context(db, conversation_id, NOW + timedelta(days=8)) == []
    db.commit()
    db.expunge_all()
    assert db.get(Conversation, conversation_id).state["analysis_turns"] == []


def test_read_receipt_allows_analysis_memory(db):
    source = envelope(owner(db))
    result = DialogueService().process(db, source, lambda *_args: response(source))
    outbox = db.get(OutboxMessage, result.outbox_message_id)
    outbox.state = DeliveryState.READ.value
    service = DialogueService()
    assert service.remember_analysis(
        db,
        source.conversation_id,
        operation_id=result.operation_id,
        outbox_id=outbox.id,
        expected_epoch=service.begin_generation(db, source.conversation_id),
        question="hello",
        answer="done",
    )


def test_edited_answer_replaces_retained_operation_revision(db):
    person = owner(db)
    conversation_id = uuid4()
    service = DialogueService()
    original = envelope(person, conversation_id=conversation_id)
    first = service.process(db, original, lambda *_args: response(original, "old answer"))
    first_outbox = db.get(OutboxMessage, first.outbox_message_id)
    first_outbox.state = DeliveryState.DELIVERED.value
    epoch = service.begin_generation(db, conversation_id)
    now = datetime.now(UTC)
    assert service.remember_analysis(
        db,
        conversation_id,
        operation_id=first.operation_id,
        outbox_id=first_outbox.id,
        expected_epoch=epoch,
        question="hello",
        answer="old answer",
    )
    edited = envelope(
        person,
        conversation_id=conversation_id,
        message_id=uuid4(),
        external_event_id="edited",
        kind=InboundKind.EDIT,
        revision=2,
        text="corrected",
    )
    second = service.process(db, edited, lambda *_args: response(edited, "new answer"))
    second_outbox = db.get(OutboxMessage, second.outbox_message_id)
    second_outbox.state = DeliveryState.DELIVERED.value
    assert service.remember_analysis(
        db,
        conversation_id,
        operation_id=second.operation_id,
        outbox_id=second_outbox.id,
        expected_epoch=epoch,
        question="corrected",
        answer="new answer",
    )
    turns = service.analysis_context(db, conversation_id, now + timedelta(seconds=2))
    assert len(turns) == 1 and turns[0]["revision"] == 2
    assert turns[0]["question"] == "corrected" and turns[0]["answer"] == "new answer"
    assert not service.remember_analysis(
        db,
        conversation_id,
        operation_id=first.operation_id,
        outbox_id=first_outbox.id,
        expected_epoch=epoch,
        question="hello",
        answer="old answer",
    )


def test_older_answer_delivery_preserves_newer_analysis_turn(db):
    person = owner(db)
    conversation_id = uuid4()
    service = DialogueService()
    now = datetime.now(UTC) - timedelta(minutes=10)
    results = []
    for index in range(7):
        source = envelope(
            person,
            conversation_id=conversation_id,
            message_id=uuid4(),
            external_event_id=f"analysis-{index}",
            external_message_id=f"message-{index}",
            text=f"question-{index}",
            received_at=now + timedelta(minutes=index),
        )
        result = service.process(
            db,
            source,
            lambda *_args, source=source, index=index: response(source, f"answer-{index}"),
        )
        db.get(OutboxMessage, result.outbox_message_id).state = DeliveryState.DELIVERED.value
        results.append(result)
    epoch = service.begin_generation(db, conversation_id)
    for index in (*range(1, 7), 0):
        result = results[index]
        assert service.remember_analysis(
            db,
            conversation_id,
            operation_id=result.operation_id,
            outbox_id=result.outbox_message_id,
            expected_epoch=epoch,
            question=f"question-{index}",
            answer=f"answer-{index}",
        )
    turns = service.analysis_context(db, conversation_id, now + timedelta(minutes=7))
    assert [turn["question"] for turn in turns] == [f"question-{index}" for index in range(1, 7)]


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
            inbound_message_id=row.id,
        )
    assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 0


def test_analysis_memory_matches_confirmed_inbound_and_outbound(db):
    source = envelope(owner(db))
    result = DialogueService().process(db, source, lambda *_args: response(source))
    db.get(OutboxMessage, result.outbox_message_id).state = DeliveryState.DELIVERED.value
    service = DialogueService()
    with pytest.raises(Conflict, match="must match"):
        service.remember_analysis(
            db,
            source.conversation_id,
            operation_id=result.operation_id,
            outbox_id=result.outbox_message_id,
            expected_epoch=service.begin_generation(db, source.conversation_id),
            question="fabricated",
            answer="done",
        )


def test_generated_answer_keeps_authenticated_inbound_link(db):
    source = envelope(owner(db))
    inbound, _ = ingest_envelope(db, source)
    service = DialogueService()
    epoch = service.begin_generation(db, source.conversation_id)

    outbox = service.queue_generation_result(
        db,
        response(source),
        expected_epoch=epoch,
        operation_id=inbound.operation_id,
        inbound_message_id=inbound.id,
    )

    assert outbox.inbound_message_id == inbound.id
    outbox.state = DeliveryState.DELIVERED.value
    assert service.remember_analysis(
        db,
        source.conversation_id,
        operation_id=inbound.operation_id,
        outbox_id=outbox.id,
        expected_epoch=epoch,
        question="hello",
        answer="done",
    )


def test_stale_generated_revision_cannot_replace_edited_answer(db):
    person = owner(db)
    source = envelope(person)
    first, _ = ingest_envelope(db, source)
    service = DialogueService()
    epoch = service.begin_generation(db, source.conversation_id)
    edited = envelope(
        person,
        conversation_id=source.conversation_id,
        external_event_id="edited",
        message_id=uuid4(),
        kind=InboundKind.EDIT,
        revision=2,
        text="corrected",
    )
    second, _ = ingest_envelope(db, edited)
    assert first.operation_id == second.operation_id

    assert (
        service.queue_generation_result(
            db,
            response(source, "old answer"),
            expected_epoch=epoch,
            operation_id=first.operation_id,
            inbound_message_id=first.id,
        )
        is None
    )
    current = service.queue_generation_result(
        db,
        response(edited, "new answer"),
        expected_epoch=epoch,
        operation_id=second.operation_id,
        inbound_message_id=second.id,
    )
    assert current.inbound_message_id == second.id
    assert current.dedup_key.endswith(":revision:2:reply")
    assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 1


def test_shared_memory_revocation_cancels_queued_generated_answer(db):
    person = owner(db)
    source, _ = ingest_envelope(db, envelope(person, external_event_id="source"))
    target, _ = ingest_envelope(db, envelope(person, external_event_id="target"))
    service = DialogueService()
    service.set_owner_memory_sharing(db, source.conversation_id, True)
    service.set_owner_memory_sharing(db, target.conversation_id, True)
    _turns, target_epoch, source_epochs = service.analysis_snapshot(db, target.conversation_id, NOW)
    target_intent = response(envelope(person, conversation_id=target.conversation_id))
    queued = service.queue_generation_result(
        db,
        target_intent,
        expected_epoch=target_epoch,
        operation_id=target.operation_id,
        inbound_message_id=target.id,
        source_epochs=source_epochs,
    )
    assert queued is not None
    service.forget(db, source.conversation_id)

    assert claim_outbox(db, NOW) is None
    assert queued.state == DeliveryState.CANCELLED.value


def test_delivered_generated_answer_cannot_be_retained_after_source_forget(db):
    person = owner(db)
    source, _ = ingest_envelope(db, envelope(person, external_event_id="source"))
    target, _ = ingest_envelope(db, envelope(person, external_event_id="target"))
    service = DialogueService()
    service.set_owner_memory_sharing(db, source.conversation_id, True)
    service.set_owner_memory_sharing(db, target.conversation_id, True)
    _turns, target_epoch, source_epochs = service.analysis_snapshot(db, target.conversation_id, NOW)
    queued = service.queue_generation_result(
        db,
        response(envelope(person, conversation_id=target.conversation_id)),
        expected_epoch=target_epoch,
        operation_id=target.operation_id,
        inbound_message_id=target.id,
        source_epochs=source_epochs,
    )
    queued.state = DeliveryState.DELIVERED.value
    service.forget(db, source.conversation_id)

    assert not service.remember_analysis(
        db,
        target.conversation_id,
        operation_id=target.operation_id,
        outbox_id=queued.id,
        expected_epoch=target_epoch,
        question="hello",
        answer="done",
    )
    assert service.analysis_context(db, target.conversation_id, NOW) == []


def test_retained_shared_answer_disappears_when_source_sharing_is_revoked(db):
    person = owner(db)
    source, _ = ingest_envelope(db, envelope(person, external_event_id="source"))
    target, _ = ingest_envelope(db, envelope(person, external_event_id="target"))
    service = DialogueService()
    service.set_owner_memory_sharing(db, source.conversation_id, True)
    service.set_owner_memory_sharing(db, target.conversation_id, True)
    _turns, target_epoch, source_epochs = service.analysis_snapshot(db, target.conversation_id, NOW)
    queued = service.queue_generation_result(
        db,
        response(envelope(person, conversation_id=target.conversation_id)),
        expected_epoch=target_epoch,
        operation_id=target.operation_id,
        inbound_message_id=target.id,
        source_epochs=source_epochs,
    )
    queued.state = DeliveryState.DELIVERED.value
    assert service.remember_analysis(
        db,
        target.conversation_id,
        operation_id=target.operation_id,
        outbox_id=queued.id,
        expected_epoch=target_epoch,
        source_epochs=source_epochs,
        question="hello",
        answer="done",
    )
    assert service.analysis_context(db, target.conversation_id, NOW)
    service.set_owner_memory_sharing(db, source.conversation_id, False)
    assert service.analysis_context(db, target.conversation_id, NOW) == []


def test_edit_after_generation_cancels_queued_answer_and_prevents_retention(db):
    person = owner(db)
    first, _ = ingest_envelope(db, envelope(person))
    service = DialogueService()
    epoch = service.begin_generation(db, first.conversation_id)
    queued = service.queue_generation_result(
        db,
        response(envelope(person, conversation_id=first.conversation_id)),
        expected_epoch=epoch,
        operation_id=first.operation_id,
        inbound_message_id=first.id,
    )
    ingest_envelope(
        db,
        envelope(
            person,
            conversation_id=first.conversation_id,
            external_event_id="edited",
            message_id=uuid4(),
            kind=InboundKind.EDIT,
            revision=2,
            text="corrected",
        ),
    )
    assert claim_outbox(db, NOW) is None
    assert queued.state == DeliveryState.CANCELLED.value
    queued.state = DeliveryState.DELIVERED.value
    assert not service.remember_analysis(
        db,
        first.conversation_id,
        operation_id=first.operation_id,
        outbox_id=queued.id,
        expected_epoch=epoch,
        question="hello",
        answer="done",
    )


def test_context_invalidation_clears_neutral_analysis_memory(db):
    from garmin_ai.replay import invalidate_outputs
    from garmin_ai.share_policy import _forget_model_context

    row, _ = ingest_envelope(db, envelope(owner(db)))
    conversation = db.get(Conversation, row.conversation_id)
    old_epoch = conversation.memory_epoch
    conversation.state = {"analysis_turns": [{"question": "synthetic", "answer": "synthetic"}]}
    _forget_model_context(db)
    assert conversation.memory_epoch != old_epoch
    assert "analysis_turns" not in conversation.state

    old_epoch = conversation.memory_epoch
    conversation.state = {"analysis_turns": [{"question": "synthetic", "answer": "synthetic"}]}
    invalidate_outputs(db)
    assert conversation.memory_epoch != old_epoch
    assert "analysis_turns" not in conversation.state


def test_old_inbound_cannot_be_retained_with_later_completion(db):
    source = envelope(
        owner(db),
        received_at=datetime.now(UTC) - timedelta(days=8),
    )
    result = DialogueService().process(db, source, lambda *_args: response(source))
    db.get(OutboxMessage, result.outbox_message_id).state = DeliveryState.DELIVERED.value
    service = DialogueService()

    assert not service.remember_analysis(
        db,
        source.conversation_id,
        operation_id=result.operation_id,
        outbox_id=result.outbox_message_id,
        expected_epoch=service.begin_generation(db, source.conversation_id),
        question="hello",
        answer="done",
    )
    assert service.analysis_context(db, source.conversation_id, datetime.now(UTC)) == []


def test_shared_analysis_snapshot_fences_source_forget_and_revocation(db):
    person = owner(db)
    service = DialogueService()
    source, _ = ingest_envelope(db, envelope(person, external_event_id="shared-source"))
    target, _ = ingest_envelope(db, envelope(person, external_event_id="shared-target"))
    service.set_owner_memory_sharing(db, source.conversation_id, True)
    service.set_owner_memory_sharing(db, target.conversation_id, True)
    _turns, target_epoch, source_epochs = service.analysis_snapshot(db, target.conversation_id, NOW)
    assert source_epochs == {
        source.conversation_id: service.begin_generation(db, source.conversation_id)
    }
    intent = response(envelope(person, conversation_id=target.conversation_id))

    service.forget(db, source.conversation_id)
    assert (
        service.queue_generation_result(
            db,
            intent,
            expected_epoch=target_epoch,
            source_epochs=source_epochs,
            operation_id=uuid4(),
            inbound_message_id=target.id,
        )
        is None
    )
    _turns, target_epoch, source_epochs = service.analysis_snapshot(db, target.conversation_id, NOW)
    service.set_owner_memory_sharing(db, source.conversation_id, False)
    assert (
        service.queue_generation_result(
            db,
            intent,
            expected_epoch=target_epoch,
            source_epochs=source_epochs,
            operation_id=uuid4(),
            inbound_message_id=target.id,
        )
        is None
    )
    assert (
        service.queue_generation_result(
            db,
            intent,
            expected_epoch=target_epoch,
            operation_id=uuid4(),
            inbound_message_id=target.id,
        )
        is None
    )


def test_analysis_context_excludes_future_turns_and_scheduled_prune_expires_idle_memory(db):
    row, _ = ingest_envelope(db, envelope(owner(db)))
    conversation = db.get(Conversation, row.conversation_id)
    conversation.state = {
        "analysis_turns": [
            {"operation_id": "old", "asked_at": NOW.isoformat(), "question": "old"},
            {
                "operation_id": "future",
                "asked_at": (NOW + timedelta(days=1)).isoformat(),
                "question": "future",
            },
        ]
    }
    service = DialogueService()
    assert [
        turn["question"] for turn in service.analysis_context(db, row.conversation_id, NOW)
    ] == ["old"]
    assert prune_neutral_analysis(db, NOW + timedelta(days=8)) == 1
    assert conversation.state["analysis_turns"] == []


def test_scheduled_prune_skips_busy_writer_without_waiting(db, db_engine):
    row, _ = ingest_envelope(db, envelope(owner(db)))
    db.get(Conversation, row.conversation_id).state = {
        "analysis_turns": [{"operation_id": "old", "asked_at": NOW.isoformat(), "question": "old"}]
    }
    db.commit()

    with db_engine.connect() as blocker, blocker.begin():
        blocker.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        assert prune_neutral_analysis(db, NOW + timedelta(days=8)) == 0


def test_analysis_context_sorts_offsets_by_instant(db):
    row, _ = ingest_envelope(db, envelope(owner(db)))
    conversation = db.get(Conversation, row.conversation_id)
    conversation.state = {
        "analysis_turns": [
            {
                "operation_id": "later",
                "asked_at": NOW.replace(hour=11)
                .astimezone(timezone(timedelta(hours=2)))
                .isoformat(),
                "question": "later",
            },
            {
                "operation_id": "earlier",
                "asked_at": NOW.replace(hour=10).isoformat(),
                "question": "earlier",
            },
        ]
    }
    assert [
        turn["question"]
        for turn in DialogueService().analysis_context(db, row.conversation_id, NOW)
    ] == ["earlier", "later"]


def test_analysis_memory_enforces_combined_utf8_byte_limit(db):
    person = owner(db)
    conversation_id = uuid4()
    service = DialogueService()
    for index in range(2):
        source = envelope(
            person,
            conversation_id=conversation_id,
            message_id=uuid4(),
            external_event_id=f"utf8-{index}",
            external_message_id=f"utf8-message-{index}",
            text="界" * 1000,
        )
        result = service.process(
            db, source, lambda *_args, source=source: response(source, "界" * 1500)
        )
        db.get(OutboxMessage, result.outbox_message_id).state = DeliveryState.DELIVERED.value
        assert service.remember_analysis(
            db,
            conversation_id,
            operation_id=result.operation_id,
            outbox_id=result.outbox_message_id,
            expected_epoch=service.begin_generation(db, conversation_id),
            question=source.text,
            answer="界" * 1500,
        )
    turns = db.get(Conversation, conversation_id).state["analysis_turns"]
    assert len(turns) == 1
    assert len(json.dumps(turns, ensure_ascii=False).encode("utf-8")) <= 12_000


def test_analysis_memory_marks_truncated_snippets(db):
    source = envelope(owner(db), text="Q" * 1001)
    result = DialogueService().process(db, source, lambda *_args: response(source, "A" * 1501))
    db.get(OutboxMessage, result.outbox_message_id).state = DeliveryState.DELIVERED.value
    service = DialogueService()
    assert service.remember_analysis(
        db,
        source.conversation_id,
        operation_id=result.operation_id,
        outbox_id=result.outbox_message_id,
        expected_epoch=service.begin_generation(db, source.conversation_id),
        question=source.text,
        answer="A" * 1501,
    )
    turn = service.analysis_context(db, source.conversation_id, datetime.now(UTC))[0]
    assert turn["question_truncated"] and turn["answer_truncated"]
    assert len(turn["question"]) == 1000 and len(turn["answer"]) == 1500


def test_shared_context_has_one_combined_utf8_limit(db):
    person = owner(db)
    service = DialogueService()
    target, _ = ingest_envelope(db, envelope(person, external_event_id="bounded-target"))
    for index in range(3):
        source, _ = ingest_envelope(db, envelope(person, external_event_id=f"bounded-{index}"))
        conversation = db.get(Conversation, source.conversation_id)
        conversation.state = {
            "analysis_turns": [
                {
                    "operation_id": str(uuid4()),
                    "asked_at": (NOW + timedelta(seconds=index)).isoformat(),
                    "question": "界" * 2500,
                    "answer": "yes",
                }
            ]
        }
        service.set_owner_memory_sharing(db, source.conversation_id, True)
    service.set_owner_memory_sharing(db, target.conversation_id, True)

    turns, _target_epoch, _source_epochs = service.analysis_snapshot(
        db, target.conversation_id, NOW + timedelta(minutes=1)
    )

    assert len(turns) == 1
    assert len(json.dumps(turns, ensure_ascii=False).encode("utf-8")) <= 12_000


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


def test_older_failure_receipt_does_not_override_newer_provider_acceptance(db):
    from datetime import timedelta

    person = owner(db)
    source = envelope(person)
    result = DialogueService().process(db, source, lambda *_args: response(source))
    for state, observed_at in (
        (DeliveryState.PROVIDER_ACCEPTED, NOW),
        (DeliveryState.FAILED, NOW - timedelta(minutes=1)),
    ):
        record_delivery_receipt(
            db,
            result.outbox_message_id,
            DeliveryReceipt(
                intent_id=result.outbox_message_id,
                state=state,
                observed_at=observed_at,
                provider_reference="opaque-provider-ref",
            ),
        )
    assert (
        db.get(OutboxMessage, result.outbox_message_id).state
        == DeliveryState.PROVIDER_ACCEPTED.value
    )


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
