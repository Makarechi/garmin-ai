from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from telegram.error import BadRequest, NetworkError, RetryAfter

from garmin_ai.channels import (
    ActionRef,
    AttachmentRef,
    ChannelInstanceRef,
    DeliveryState,
    InboundKind,
    OutboundIntent,
    TextBlock,
)
from garmin_ai.config import Settings
from garmin_ai.dialogue import queue_intent
from garmin_ai.models import AppState, InboundMessage, OutboxMessage, Person, TelegramUpdate
from garmin_ai.telegram import handle_button, process_message, save_update, scenario_keyboard
from garmin_ai.telegram_adapter import (
    TELEGRAM_INSTANCE,
    TelegramChannel,
    normalize_update,
    record_neutral_ingress,
    set_update_status,
)
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)


def update(**message_changes):
    message = {
        "message_id": 7,
        "date": 1_789_000_000,
        "from": {"id": 42},
        "chat": {"id": 42, "type": "private"},
        "text": "synthetic diary text",
    }
    message.update(message_changes)
    return {"update_id": 11, "message": message}


def test_normalization_authenticates_before_creating_neutral_envelope():
    internal_owner = uuid4()
    now = datetime.now(UTC)
    envelope = normalize_update(
        update(reply_to_message={"message_id": 3}),
        external_owner_id=42,
        internal_owner_id=internal_owner,
        received_at=now,
    )

    assert envelope.owner_id == internal_owner
    assert envelope.channel_instance == TELEGRAM_INSTANCE
    assert envelope.external_event_id == "11"
    assert envelope.external_message_id == "7"
    assert envelope.reply_to.external_message_id == "3"
    assert envelope.text == "synthetic diary text"

    foreign = update()
    foreign["message"]["from"]["id"] = 99
    with pytest.raises(PermissionError):
        normalize_update(
            foreign,
            external_owner_id=42,
            internal_owner_id=internal_owner,
            received_at=now,
        )


def test_normalization_preserves_configured_channel_instance(db):
    configured = ChannelInstanceRef(channel="telegram", instance_id="private")
    envelope = normalize_update(
        update(),
        external_owner_id=42,
        internal_owner_id=uuid4(),
        received_at=datetime.now(UTC),
        channel_instance=configured,
    )

    assert envelope.channel_instance == configured
    assert envelope.reply_to is None
    assert save_update(db, update(), 42, channel_instance=configured)
    stored = db.scalar(select(InboundMessage))
    assert stored.channel_instance_id == "private"


def test_captionless_unsupported_media_is_recorded_without_blocking_ingress(db):
    item = update()
    item["message"].pop("text")
    item["message"]["photo"] = [{"file_id": "opaque-photo"}]
    now = datetime.now(UTC)

    envelope = normalize_update(
        item,
        external_owner_id=42,
        internal_owner_id=uuid4(),
        received_at=now,
    )
    row, created = record_neutral_ingress(db, item, 42, now)

    assert envelope.kind is InboundKind.SYSTEM
    assert envelope.text is None
    assert created
    assert row.kind == InboundKind.SYSTEM


def test_voice_and_legacy_callback_have_explicit_neutral_shapes():
    owner_id = uuid4()
    now = datetime.now(UTC)
    voice = update(text=None, voice={"file_id": "opaque-file", "file_size": 123})
    voice_envelope = normalize_update(
        voice,
        external_owner_id=42,
        internal_owner_id=owner_id,
        received_at=now,
    )
    assert voice_envelope.kind == "voice"
    assert voice_envelope.attachments[0].external_id == "opaque-file"

    callback = {
        "update_id": 12,
        "callback_query": {
            "id": "opaque-callback",
            "from": {"id": 42},
            "data": "coffee",
            "message": update()["message"],
        },
    }
    action = normalize_update(
        callback,
        external_owner_id=42,
        internal_owner_id=owner_id,
        received_at=now,
    )
    assert action.kind == "action"
    assert action.action.action_id == "coffee"
    assert action.occurred_at is None
    assert action.time_precision == "unknown"

    callback["_callback_time_known"] = True
    current_action = normalize_update(
        callback,
        external_owner_id=42,
        internal_owner_id=owner_id,
        received_at=now,
    )
    assert current_action.occurred_at == now
    assert current_action.time_precision == "second"


def test_legacy_ingress_dual_write_is_idempotent_and_statuses_stay_aligned(db):
    item = update()
    assert save_update(db, item, 42)
    assert save_update(db, item, 42)
    db.flush()

    assert db.scalar(select(func.count()).select_from(TelegramUpdate)) == 1
    assert db.scalar(select(func.count()).select_from(InboundMessage)) == 1
    neutral = db.scalar(select(InboundMessage))
    assert neutral.legacy_telegram_update_id == 11
    assert neutral.owner_id == db.scalar(select(Person.id))

    set_update_status(db, 11, "processed")
    assert db.get(TelegramUpdate, 11).status == "processed"
    assert neutral.status == "processed"


def test_durable_action_token_is_persisted_and_single_use(db):
    now = datetime.now(UTC)
    inbound, _ = record_neutral_ingress(db, update(), 42, now)
    operation_id = uuid4()
    queued = queue_intent(
        db,
        OutboundIntent(
            owner_id=inbound.owner_id,
            conversation_id=inbound.conversation_id,
            channel_instance=TELEGRAM_INSTANCE,
            blocks=[TextBlock(text="Choose")],
            actions=[
                ActionRef(
                    action_id="confirm",
                    label="Confirm",
                    operation_id=operation_id,
                )
            ],
        ),
        operation_id=operation_id,
        inbound_message_id=inbound.id,
    )
    token = queued.intent["actions"][0]["token"]
    assert token and db.get(OutboxMessage, queued.id).intent["actions"][0]["token"] == token

    callback = {
        "update_id": 12,
        "callback_query": {
            "id": "opaque-callback",
            "from": {"id": 42},
            "data": token,
            "message": update()["message"],
        },
    }
    action_row, created = record_neutral_ingress(db, callback, 42, now)
    assert created
    assert action_row.operation_id == operation_id
    assert action_row.envelope["action"]["action_id"] == "confirm"
    assert queued.intent["actions"][0]["token"] is None

    callback["update_id"] = 13
    with pytest.raises(LookupError, match="unavailable"):
        record_neutral_ingress(db, callback, 42, now)


def test_resolved_action_reaches_active_telegram_dispatcher(db, db_engine):
    now = datetime.now(UTC)
    inbound, _ = record_neutral_ingress(db, update(), 42, now)
    queued = queue_intent(
        db,
        OutboundIntent(
            owner_id=inbound.owner_id,
            conversation_id=inbound.conversation_id,
            channel_instance=TELEGRAM_INSTANCE,
            blocks=[TextBlock(text="Choose")],
            actions=[ActionRef(action_id="coffee", label="Coffee", operation_id=uuid4())],
        ),
        operation_id=uuid4(),
        inbound_message_id=inbound.id,
    )
    token = queued.intent["actions"][0]["token"]
    callback = {
        "update_id": 12,
        "callback_query": {
            "id": "opaque-callback",
            "from": {"id": 42},
            "data": token,
            "message": update()["message"],
        },
    }

    assert save_update(db, callback, 42)
    assert db.get(TelegramUpdate, 12).payload["callback_query"]["data"] == "coffee"
    db.commit()

    response = process_message(db_engine, None, Settings(telegram_user_id=42), 12)
    assert "Время нажатия кнопки неизвестно" in response


def test_dispatcher_version_keeps_exactly_one_legacy_consumer(db):
    assert save_update(db, update(), 42, dispatcher_version="legacy-v1")
    db.flush()

    assert db.scalar(select(func.count()).select_from(TelegramUpdate)) == 1
    assert db.scalar(select(func.count()).select_from(InboundMessage)) == 0


def test_shadow_dispatcher_rejects_edited_messages_instead_of_replaying_them(db):
    item = update()
    item["edited_message"] = {
        **item.pop("message"),
        "edit_date": 1_789_000_100,
        "text": "corrected diary text",
    }

    assert not save_update(db, item, 42)
    db.flush()

    assert db.scalar(select(func.count()).select_from(TelegramUpdate)) == 0
    assert db.scalar(select(func.count()).select_from(InboundMessage)) == 0


def test_generated_tracker_appears_in_menu_and_opens_without_telegram_branch(db):
    draft = TrackerSetupDraft(
        key="focus",
        name="Фокус",
        locale="ru",
        topology="point",
        fields=[
            TrackerFieldDraft(
                key="quality",
                label="Качество",
                kind="scale",
                minimum=1,
                maximum=5,
            )
        ],
        shortcut="Записать фокус",
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )

    keyboard = scenario_keyboard(db)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    generated = next(button for button in buttons if button.text == "Записать фокус")
    assert generated.callback_data == created["action"]["id"]

    response = handle_button(
        db,
        generated.callback_data,
        SimpleNamespace(),
        "telegram:42",
        12,
        datetime.now(UTC),
    )
    pending = db.get(AppState, "conversation:pending")
    assert "Качество" in response
    assert pending.value["button"] == "tracker_form"
    assert pending.value["definition_version_id"] == str(created["action"]["definition_version_id"])


def intent(**changes):
    operation_id = uuid4()
    values = dict(
        owner_id=uuid4(),
        conversation_id=uuid4(),
        channel_instance=TELEGRAM_INSTANCE,
        blocks=[TextBlock(text="Choose")],
        actions=[
            ActionRef(
                action_id="confirm",
                label="Confirm",
                operation_id=operation_id,
                token="single-use-token-123456",
            )
        ],
    )
    values.update(changes)
    return OutboundIntent(**values)


def test_telegram_channel_renders_actions_and_reports_only_provider_acceptance():
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id="opaque-provider-message")

    adapter = TelegramChannel(Bot(), 42)
    result = __import__("asyncio").run(adapter.deliver(intent(), now=datetime.now(UTC)))

    assert result.state is DeliveryState.PROVIDER_ACCEPTED
    assert not result.receipt.confirms_delivery
    assert calls[0]["reply_markup"].inline_keyboard[0][0].callback_data


def test_telegram_channel_keeps_ambiguous_and_unsupported_delivery_explicit():
    class Bot:
        async def send_message(self, **kwargs):
            raise NetworkError("synthetic disconnect")

    adapter = TelegramChannel(Bot(), 42)
    uncertain = __import__("asyncio").run(adapter.deliver(intent(), now=datetime.now(UTC)))
    assert uncertain.state is DeliveryState.UNCERTAIN

    unsupported = __import__("asyncio").run(
        adapter.deliver(
            intent(
                actions=[],
                attachments=[AttachmentRef(kind="file", external_id="opaque-file")],
            ),
            now=datetime.now(UTC),
        )
    )
    assert unsupported.state is DeliveryState.QUEUED
    assert "not implemented" in unsupported.reason


def test_telegram_channel_fences_partial_multi_chunk_delivery():

    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise BadRequest("synthetic second chunk rejection")
            return SimpleNamespace(message_id="accepted-first-chunk")

    adapter = TelegramChannel(Bot(), 42)
    attempt = __import__("asyncio").run(
        adapter.deliver(
            intent(blocks=[TextBlock(text="a" * 3501)], actions=[]),
            now=datetime.now(UTC),
        )
    )

    assert attempt.state is DeliveryState.UNCERTAIN
    assert attempt.receipt.provider_reference == "accepted-first-chunk"


def test_telegram_channel_uses_delivery_clock_for_action_expiry():
    now = datetime(2026, 9, 20, tzinfo=UTC)
    item = intent()
    item = item.model_copy(
        update={"actions": [item.actions[0].model_copy(update={"expires_at": now})]}
    )

    class Bot:
        async def send_message(self, **kwargs):
            raise AssertionError("Expired action must not be sent")

    result = __import__("asyncio").run(TelegramChannel(Bot(), 42).deliver(item, now=now))
    assert result.state is DeliveryState.EXPIRED


def test_telegram_channel_rejects_oversized_callback_data_before_delivery():
    class Bot:
        async def send_message(self, **kwargs):
            raise AssertionError("An oversized callback token must not reach Telegram")

    item = intent()
    item = item.model_copy(
        update={"actions": [item.actions[0].model_copy(update={"token": "x" * 65})]}
    )
    result = __import__("asyncio").run(
        TelegramChannel(Bot(), 42).deliver(item, now=datetime.now(UTC))
    )
    assert result.state is DeliveryState.FAILED
    assert "64-byte" in result.reason


def test_rate_limit_after_first_chunk_is_not_requeued():
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise RetryAfter(5)
            return SimpleNamespace(message_id=7)

    item = intent(actions=[], blocks=[TextBlock(text="first"), TextBlock(text="second")])
    result = __import__("asyncio").run(
        TelegramChannel(Bot(), 42).deliver(item, now=datetime.now(UTC))
    )

    assert result.state is DeliveryState.UNCERTAIN
    assert result.retry_after is None
    assert len(calls) == 2
