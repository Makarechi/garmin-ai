"""Exercise tracker writes through actual HTTP, Telegram and reference ingress."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from garmin_ai.accounts import owner
from garmin_ai.api import create_app
from garmin_ai.channels import ChannelInstanceRef, DeliveryState, OutboundIntent, TextBlock
from garmin_ai.config import ApiToken, Settings
from garmin_ai.dialogue import (
    CommandDispatcher,
    CommandRequest,
    DialogueService,
    record_delivery_receipt,
)
from garmin_ai.models import AppState, Audit, Event, OutboxMessage
from garmin_ai.restricted_channel import RESTRICTED_INSTANCE, RestrictedTextChannel
from garmin_ai.telegram import handle_button, process_message, save_update
from garmin_ai.telegram_history import history_page, selected_action
from garmin_ai.tracker_forms import (
    FormSubmission,
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    action_for_event,
    confirm_tracker,
    form_for_action,
    preview_tracker,
    submit_form,
)


def _tracker(db):
    draft = TrackerSetupDraft(
        key="entrypoint_parity",
        name="Entrypoint parity",
        locale="en",
        fields=[TrackerFieldDraft(key="score", label="Score", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="synthetic-test",
    )
    return form_for_action(db, created["action"]["id"], locale="en")


def _submission(form, now):
    return FormSubmission(
        action_id=form.id,
        schema_hash=form.schema_hash,
        submission_id=form.submission_id,
        start=now,
        timezone="UTC",
        values={"score": 4},
        units={"score": "score_1-5"},
    )


@pytest.mark.anyio
@pytest.mark.parametrize("entry_point", ["http", "telegram", "restricted"])
async def test_create_retries_have_one_fact_and_audit_via_actual_ingress(
    db, db_engine, entry_point
):
    form = _tracker(db)
    now = datetime.now(UTC)
    db.commit()
    if entry_point == "http":
        key = "synthetic-parity-token-" + "x" * 32
        client = TestClient(
            create_app(
                Settings(api_tokens=[ApiToken(key=key, scopes={"read:diary", "write:diary"})]),
                db_engine,
            )
        )
        headers = {"Authorization": "Bearer " + key}
        body = _submission(form, now).model_dump(mode="json")
        first = client.post(f"/forms/{form.id}/submit", json=body, headers=headers)
        second = client.post(f"/forms/{form.id}/submit", json=body, headers=headers)
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
    elif entry_point == "telegram":
        db.info["channel_destination_instance_id"] = "telegram:primary"
        assert "Когда" in handle_button(
            db, form.id, Settings(telegram_user_id=42), "telegram:42", 9000, now
        )
        db.commit()
        for update_id, answer in [(9001, "now"), (9002, "4")]:
            update = {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": answer,
                },
            }
            assert save_update(db, update, 42)
            db.commit()
            result = process_message(db_engine, None, Settings(telegram_user_id=42), update_id)
            assert result
            assert (
                process_message(db_engine, None, Settings(telegram_user_id=42), update_id) == result
            )
        assert "Запись сохранена" in result
    else:
        channel = RestrictedTextChannel()
        conversation_id = uuid4()
        source = channel.receive_text(
            owner_id=owner(db).id,
            conversation_id=conversation_id,
            external_event_id="opaque:form-submit",
            sender_ref="synthetic-owner",
            text=json.dumps(_submission(form, now).model_dump(mode="json")),
            received_at=now,
        )
        dispatcher = CommandDispatcher()

        def submit(session, actor, arguments):
            event = submit_form(
                session,
                arguments["action_id"],
                FormSubmission.model_validate(arguments),
                actor=f"restricted:{actor.operation_id}",
            )
            return OutboundIntent(
                owner_id=actor.owner_id,
                conversation_id=actor.conversation_id,
                channel_instance=RESTRICTED_INSTANCE,
                blocks=[TextBlock(text=f"Saved {event.id}")],
            )

        dispatcher.register("tracker.submit", submit, permissions=frozenset({"write:diary"}))

        def handler(session, actor, incoming):
            return dispatcher.dispatch(
                session,
                actor,
                CommandRequest(name="tracker.submit", arguments=json.loads(incoming.text)),
            )

        service = DialogueService()
        first = service.process(db, source, handler, permissions=frozenset({"write:diary"}))
        db.commit()
        assert first.outbox_message_id is not None
        outbox = db.get(OutboxMessage, first.outbox_message_id)
        accepted = await channel.deliver(OutboundIntent.model_validate(outbox.intent), now=now)
        record_delivery_receipt(db, outbox.id, accepted.receipt)
        delivered = channel.confirm_delivery(accepted.receipt.provider_reference, now=now)
        record_delivery_receipt(db, outbox.id, delivered)
        assert outbox.state == DeliveryState.DELIVERED.value
        db.commit()
        for _ in range(10):
            retried = service.process(
                db, source.model_copy(update={"message_id": uuid4()}), handler
            )
            assert retried.duplicate and retried.outbox_message_id == first.outbox_message_id
        assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 1

    db.expire_all()
    events = db.scalars(select(Event).where(Event.kind == "user.entrypoint_parity")).all()
    assert len(events) == 1 and events[0].payload["score"] == 4
    assert (
        db.scalar(select(func.count()).select_from(Audit).where(Audit.event_id == events[0].id))
        == 1
    )


@pytest.mark.anyio
@pytest.mark.parametrize("entry_point", ["http", "telegram", "restricted"])
async def test_edit_uses_pinned_revision_via_actual_ingress(db, db_engine, entry_point):
    form = _tracker(db)
    now = datetime.now(UTC)
    original = submit_form(db, form.id, _submission(form, now), actor="synthetic-test")
    edit = form_for_action(db, action_for_event(db, original.id).id, locale="en")
    body = FormSubmission(
        action_id=edit.id,
        schema_hash=edit.schema_hash,
        start=now,
        timezone="UTC",
        values={"score": 5},
        units={"score": "score_1-5"},
    )
    db.commit()

    if entry_point == "http":
        key = "synthetic-parity-token-" + "x" * 32
        client = TestClient(
            create_app(
                Settings(api_tokens=[ApiToken(key=key, scopes={"read:diary", "write:diary"})]),
                db_engine,
            )
        )
        headers = {"Authorization": "Bearer " + key}
        first = client.post(
            f"/forms/{edit.id}/submit", json=body.model_dump(mode="json"), headers=headers
        )
        stale = client.post(
            f"/forms/{edit.id}/submit", json=body.model_dump(mode="json"), headers=headers
        )
        assert first.status_code == 200 and stale.status_code == 409
    elif entry_point == "telegram":
        db.info["channel_destination_instance_id"] = "telegram:primary"
        db.info["channel_instance"] = ChannelInstanceRef(channel="telegram", instance_id="primary")
        db.info["conversation_now"] = now
        db.info["locale"] = "ru"
        history_page(db, now)
        selector = next(
            row.key.removeprefix("telegram:selection:")
            for row in db.scalars(
                select(AppState).where(AppState.key.startswith("telegram:selection:"))
            )
            if row.value["action"] == "edit" and row.value["event_id"] == str(original.id)
        )
        assert "Когда" in selected_action(db, "h:" + selector, now, "telegram:42")
        db.commit()
        for update_id, answer in [(9101, "="), (9102, "5")]:
            update = {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(now.timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": answer,
                },
            }
            assert save_update(db, update, 42)
            db.commit()
            reply = process_message(db_engine, None, Settings(telegram_user_id=42), update_id)
            assert (
                process_message(db_engine, None, Settings(telegram_user_id=42), update_id) == reply
            )
        assert "Запись исправлена" in reply
    else:
        channel = RestrictedTextChannel()
        source = channel.receive_text(
            owner_id=owner(db).id,
            conversation_id=uuid4(),
            external_event_id="opaque:form-edit",
            sender_ref="synthetic-owner",
            text=json.dumps(body.model_dump(mode="json")),
            received_at=now,
        )
        dispatcher = CommandDispatcher()

        def edit_command(session, actor, arguments):
            changed = submit_form(
                session,
                arguments["action_id"],
                FormSubmission.model_validate(arguments),
                actor=f"restricted:{actor.operation_id}",
            )
            return OutboundIntent(
                owner_id=actor.owner_id,
                conversation_id=actor.conversation_id,
                channel_instance=RESTRICTED_INSTANCE,
                blocks=[TextBlock(text=f"Updated {changed.id}")],
            )

        dispatcher.register("tracker.edit", edit_command, permissions=frozenset({"write:diary"}))

        def handler(session, actor, incoming):
            return dispatcher.dispatch(
                session,
                actor,
                CommandRequest(name="tracker.edit", arguments=json.loads(incoming.text)),
            )

        service = DialogueService()
        first = service.process(db, source, handler, permissions=frozenset({"write:diary"}))
        db.commit()
        assert first.outbox_message_id is not None
        for _ in range(10):
            replay = service.process(db, source.model_copy(update={"message_id": uuid4()}), handler)
            assert replay.duplicate and replay.outbox_message_id == first.outbox_message_id

    db.expire_all()
    updated = db.get(Event, original.id)
    assert updated.revision == 2 and updated.payload["score"] == 5
    assert db.scalar(select(func.count()).select_from(Event).where(Event.kind == updated.kind)) == 1
    assert (
        db.scalar(select(func.count()).select_from(Audit).where(Audit.event_id == updated.id)) == 2
    )
