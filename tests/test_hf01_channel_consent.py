"""Consent checks across authenticated Telegram ingress and queued delivery."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.conversation import conversation_context
from garmin_ai.definitions import CustomEntryInput, create_custom_event
from garmin_ai.models import AppState, TelegramUpdate
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


@pytest.mark.parametrize("instances", [("primary", "secondary"), ("secondary", "primary")])
def test_same_provider_update_id_from_two_channel_instances_is_processed(db, db_engine, instances):
    for instance in instances:
        _ingest(db, _update(9950, "/status"), instance)
    rows = db.scalars(select(TelegramUpdate).order_by(TelegramUpdate.id)).all()
    assert len(rows) == 2
    assert {row.id for row in rows} == {9950, rows[0].id}
    assert rows[0].id < 0
    for row in rows:
        instance = row.payload["_channel_instance"]["instance_id"]
        assert "Ночной HRV" in process_message(db_engine, None, _settings(instance), row.id)


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


def test_foreign_channel_does_not_delete_pending_tracker_form(db, db_engine, sensitive_tracker):
    _grant(db, sensitive_tracker["tracker"]["definition_id"], "primary")
    _ingest(db, _callback(9961, sensitive_tracker["action"]["id"]), "primary")
    assert "Description" in process_message(db_engine, None, _settings("primary"), 9961)
    db.commit()
    pending = db.get(AppState, "conversation:pending", populate_existing=True)
    assert pending.value["channel_instance_id"] == "telegram:primary"

    _ingest(db, _update(9962, "unrelated text"), "secondary")
    process_message(db_engine, None, _settings("secondary"), 9962)
    db.expire_all()
    assert db.get(AppState, "conversation:pending") is not None


def test_schema_only_consent_keeps_form_available_for_new_input(db, db_engine, sensitive_tracker):
    definition_id = sensitive_tracker["tracker"]["definition_id"]
    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=definition_id,
            destination_kind="channel",
            destination_instance_id="telegram:primary",
            categories={"schema"},
            granted_at=datetime.now(UTC) - timedelta(minutes=1),
        ),
        authorized=True,
    )
    _ingest(db, _callback(9971, sensitive_tracker["action"]["id"]), "primary")
    assert "Description" in process_message(db_engine, None, _settings("primary"), 9971)
    db.commit()
    _ingest(db, _update(9972, "synthetic new value"), "primary")
    response = process_message(db_engine, None, _settings("primary"), 9972)
    assert "Свободный текст" in response
    assert db.get(AppState, "conversation:pending", populate_existing=True) is not None


def test_channel_revoke_keeps_unrelated_analysis_turns(db, sensitive_tracker):
    now = datetime.now(UTC)
    _grant(db, sensitive_tracker["tracker"]["definition_id"], "primary")
    db.add(
        AppState(
            key="analysis:conversation",
            value={
                "epoch": "old",
                "turns": [
                    {
                        "update_id": str(number),
                        "channel_instance_id": f"telegram:{instance}",
                        "asked_at": now.isoformat(),
                        "question": "synthetic question",
                        "answer": "synthetic answer",
                    }
                    for number, instance in ((1, "primary"), (2, "secondary"))
                ],
            },
        )
    )
    for number in (1, 2):
        db.add(AppState(key=f"outbox:update:{number}:0", value={"status": "sent"}))
        db.add(
            AppState(
                key=f"telegram:reply:{number}",
                value={
                    "kind": "analysis",
                    "status": "pending",
                    "channel_instance_id": f"telegram:{'primary' if number == 1 else 'secondary'}",
                },
            )
        )
    db.flush()

    revoke_tracker_share(
        db,
        sensitive_tracker["tracker"]["definition_id"],
        "channel",
        "telegram:primary",
        authorized=True,
    )
    db.info["channel_destination_instance_id"] = "telegram:secondary"
    assert [turn["update_id"] for turn in conversation_context(db, now)["turns"]] == ["2"]
    assert db.get(AppState, "telegram:reply:1", populate_existing=True).value["status"] == (
        "forgotten"
    )
    assert db.get(AppState, "telegram:reply:2", populate_existing=True).value["status"] == (
        "pending"
    )


def test_urgent_reply_survives_legacy_consent_guard(db, db_engine, sensitive_tracker, monkeypatch):
    from garmin_ai import telegram

    _ingest(db, _update(9980, "earlier diary request"), "primary")
    _ingest(db, _update(9981, "urgent synthetic text"), "primary")
    monkeypatch.setattr(
        telegram,
        "interpret",
        lambda *args, **kwargs: SimpleNamespace(intent="safety", clarification="unsafe echo"),
    )
    response = process_message(db_engine, object(), _settings("primary"), 9981)
    reply = db.get(AppState, "telegram:reply:9981", populate_existing=True).value
    assert "112" in response and "unsafe echo" not in response
    assert reply["channel_instance_id"] == "telegram:primary"
    assert reply["share_requirements"] == {}
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs["text"])
            return SimpleNamespace(message_id=1)

    asyncio.run(deliver(Bot(), db_engine, 42, "update:9981", response))
    assert calls == [response]


def test_tracker_clarification_rechecks_consent_before_delivery(
    db, db_engine, sensitive_tracker, monkeypatch
):
    from garmin_ai import natural_language

    definition_id = sensitive_tracker["tracker"]["definition_id"]
    _grant(db, definition_id, "primary")
    _ingest(db, _callback(9991, sensitive_tracker["action"]["id"]), "primary")
    process_message(db_engine, None, _settings("primary"), 9991)
    db.commit()
    monkeypatch.setattr(
        natural_language,
        "process_tracker_text",
        lambda *args, **kwargs: {
            "intent": "clarify",
            "clarification": "synthetic-private-value needs clarification",
        },
    )
    _ingest(db, _update(9992, "synthetic-private-value"), "primary")
    response = process_message(db_engine, None, _settings("primary"), 9992)
    reply = db.get(AppState, "telegram:reply:9992", populate_existing=True).value
    assert set(next(iter(reply["share_requirements"].values()))) == {"schema", "facts"}

    revoke_tracker_share(db, definition_id, "channel", "telegram:primary", authorized=True)
    db.commit()
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=1)

    asyncio.run(deliver(Bot(), db_engine, 42, "update:9992", response))
    assert calls == []
