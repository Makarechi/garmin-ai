"""Consent checks across authenticated Telegram ingress and queued delivery."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.conversation import conversation_context
from garmin_ai.definitions import CustomEntryInput, create_custom_event
from garmin_ai.models import AppState
from garmin_ai.queries import list_events
from garmin_ai.share_policy import (
    TrackerShareConsent,
    grant_tracker_share,
    revoke_tracker_share,
)
from garmin_ai.telegram import deliver, process_message, save_update
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)


def _update(update_id, text):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": text,
        },
    }


def _callback(update_id, data):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": 42},
            "message": {
                "message_id": update_id - 1,
                "chat": {"id": 42, "type": "private"},
            },
            "data": data,
        },
    }


def _settings(instance):
    return Settings(
        telegram_user_id=42,
        integrations=[
            IntegrationInstance(
                id=f"channel:telegram:{instance}", kind="channel", provider="telegram"
            )
        ],
    )


@pytest.fixture
def sensitive_tracker(db):
    draft = TrackerSetupDraft(
        key="hf01_private",
        name="HF01 private",
        locale="en",
        privacy="sensitive",
        fields=[
            TrackerFieldDraft(key="description", label="Description", kind="text", max_length=100)
        ],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    create_custom_event(
        db,
        CustomEntryInput(
            definition_key="user.hf01_private",
            start=datetime.now(UTC) - timedelta(minutes=1),
            timezone="UTC",
            values={"description": "synthetic-private-fact"},
        ),
        actor="test",
    )
    return created


def _grant(db, definition_id, instance):
    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=definition_id,
            destination_kind="channel",
            destination_instance_id=f"telegram:{instance}",
            categories={"schema", "facts"},
            granted_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
        authorized=True,
    )


def _ingest(db, update, instance):
    assert save_update(
        db,
        update,
        42,
        channel_instance=ChannelInstanceRef(channel="telegram", instance_id=instance),
    )
    db.commit()


@pytest.mark.parametrize("allowed", ["primary", "secondary"])
def test_history_uses_authenticated_instance_with_opposite_consents(
    db, db_engine, sensitive_tracker, allowed
):
    _grant(db, sensitive_tracker["tracker"]["definition_id"], allowed)
    for index, instance in enumerate(("primary", "secondary"), 1):
        _ingest(db, _update(9900 + index, "/history"), instance)

    for index, instance in enumerate(("primary", "secondary"), 1):
        response = process_message(db_engine, None, _settings(instance), 9900 + index)
        assert ("synthetic-private-fact" in response) == (instance == allowed)


def test_create_form_and_old_history_button_check_actual_instance(db, db_engine, sensitive_tracker):
    definition_id = sensitive_tracker["tracker"]["definition_id"]
    _grant(db, definition_id, "primary")
    action_id = sensitive_tracker["action"]["id"]

    _ingest(db, _callback(9911, action_id), "secondary")
    denied = process_message(db_engine, None, _settings("secondary"), 9911)
    assert "недоступен" in denied
    assert db.get(AppState, "conversation:pending") is None

    _ingest(db, _callback(9912, action_id), "primary")
    allowed = process_message(db_engine, None, _settings("primary"), 9912)
    assert "Description" in allowed
    assert db.get(AppState, "conversation:pending") is not None

    _ingest(db, _update(9913, "/history"), "primary")
    history = process_message(db_engine, None, _settings("primary"), 9913)
    assert "synthetic-private-fact" in history
    keyboard = db.get(AppState, "telegram:reply:9913", populate_existing=True).value["keyboard"]
    old_button = keyboard["inline_keyboard"][0][0]["callback_data"]
    _ingest(db, _callback(9914, old_button), "secondary")
    assert "устарела" in process_message(db_engine, None, _settings("secondary"), 9914)

    revoke_tracker_share(db, definition_id, "channel", "telegram:primary", authorized=True)
    db.commit()
    _ingest(db, _callback(9915, old_button), "primary")
    assert "Доступ" in process_message(db_engine, None, _settings("primary"), 9915)
    _ingest(db, _callback(9916, action_id), "primary")
    assert "недоступен" in process_message(db_engine, None, _settings("primary"), 9916)


def test_revoke_after_reply_queued_prevents_telegram_send(db, db_engine, sensitive_tracker):
    definition_id = sensitive_tracker["tracker"]["definition_id"]
    _grant(db, definition_id, "secondary")
    _ingest(db, _update(9921, "/history"), "secondary")
    response = process_message(db_engine, None, _settings("secondary"), 9921)
    assert "synthetic-private-fact" in response
    reply = db.get(AppState, "telegram:reply:9921", populate_existing=True).value
    assert reply["share_requirements"]

    revoke_tracker_share(db, definition_id, "channel", "telegram:secondary", authorized=True)
    db.commit()
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=1)

    asyncio.run(
        deliver(
            Bot(),
            db_engine,
            42,
            "update:9921",
            response,
            keyboard=reply["keyboard"],
            channel_instance=ChannelInstanceRef(channel="telegram", instance_id="secondary"),
        )
    )
    assert calls == []


def test_model_visible_history_intersects_model_and_channel_consent(db, sensitive_tracker):
    definition_id = sensitive_tracker["tracker"]["definition_id"]
    _grant(db, definition_id, "secondary")
    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=definition_id,
            destination_kind="model",
            destination_instance_id="model:gemini:primary",
            categories={"schema", "facts"},
            granted_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
        authorized=True,
    )
    db.info["llm_access"] = True
    start = datetime.now(UTC) - timedelta(minutes=2)
    end = datetime.now(UTC) + timedelta(minutes=1)
    db.info["channel_destination_instance_id"] = "telegram:primary"
    denied = list_events(db, start, end)
    assert all("synthetic-private-fact" not in str(row) for row in denied["rows"])

    db.info["channel_destination_instance_id"] = "telegram:secondary"
    allowed = list_events(db, start, end)
    assert any("synthetic-private-fact" in str(row) for row in allowed["rows"])
    assert db.info["channel_share_requirements"]


def test_retained_primary_answer_is_not_context_for_secondary(db):
    now = datetime.now(UTC)
    db.add(
        AppState(
            key="analysis:conversation",
            value={
                "epoch": None,
                "turns": [
                    {
                        "update_id": "21",
                        "asked_at": now.isoformat(),
                        "question": "synthetic private question",
                        "answer": "synthetic private answer",
                    }
                ],
            },
        )
    )
    db.add(AppState(key="outbox:update:21:0", value={"status": "sent"}))
    db.flush()
    db.info["channel_destination_instance_id"] = "telegram:secondary"
    assert conversation_context(db, now)["turns"] == []
    db.info["channel_destination_instance_id"] = "telegram:primary"
    assert len(conversation_context(db, now)["turns"]) == 1


def test_default_keyboard_is_regenerated_after_schema_revoke(db, db_engine, sensitive_tracker):
    definition_id = sensitive_tracker["tracker"]["definition_id"]
    _grant(db, definition_id, "primary")
    _ingest(db, _update(9931, "/start"), "primary")
    response = process_message(db_engine, None, _settings("primary"), 9931)
    revoke_tracker_share(db, definition_id, "channel", "telegram:primary", authorized=True)
    db.commit()
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=1)

    asyncio.run(
        deliver(
            Bot(),
            db_engine,
            42,
            "update:9931",
            response,
            keyboard=True,
            channel_instance=ChannelInstanceRef(channel="telegram", instance_id="primary"),
        )
    )
    assert len(calls) == 1
    labels = {button.text for row in calls[0]["reply_markup"].inline_keyboard for button in row}
    assert "HF01 private" not in labels
