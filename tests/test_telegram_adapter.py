from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from telegram.error import NetworkError

from garmin_ai.channels import (
    ActionRef,
    AttachmentRef,
    DeliveryState,
    OutboundIntent,
    TextBlock,
)
from garmin_ai.models import InboundMessage, Person, TelegramUpdate
from garmin_ai.telegram import save_update
from garmin_ai.telegram_adapter import (
    TELEGRAM_INSTANCE,
    TelegramChannel,
    normalize_update,
    set_update_status,
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


def test_dispatcher_version_keeps_exactly_one_legacy_consumer(db):
    assert save_update(db, update(), 42, dispatcher_version="legacy-v1")
    db.flush()

    assert db.scalar(select(func.count()).select_from(TelegramUpdate)) == 1
    assert db.scalar(select(func.count()).select_from(InboundMessage)) == 0


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
