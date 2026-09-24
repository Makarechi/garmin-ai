"""Consent checks across authenticated Telegram ingress and queued delivery."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import BigInteger, cast, select, text

from garmin_ai.agent import context_for
from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.conversation import conversation_context, is_analytic_reply
from garmin_ai.definitions import CustomEntryInput, create_custom_event, ensure_system_definitions
from garmin_ai.jobs import claim, telegram_order
from garmin_ai.models import AppState, Job, TelegramUpdate
from garmin_ai.pending_state import pending_key
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


def test_legacy_reply_guard_ignores_sensitive_system_contracts(db):
    from garmin_ai import telegram

    ensure_system_definitions(db)
    reply = AppState(key="telegram:reply:123", value={"text": "synthetic", "status": "pending"})
    db.add(reply)
    db.flush()

    assert telegram._reply_share_allowed(
        db, reply, ChannelInstanceRef(channel="telegram", instance_id="primary")
    )


def test_inline_selector_renewal_uses_delivery_channel(db, db_engine):
    before = datetime.now(UTC) - timedelta(hours=1)
    callback = "h:synthetic-channel-selector"
    pending_key_secondary = "conversation:pending:telegram:secondary"
    db.add_all(
        [
            AppState(
                key="telegram:selection:synthetic-channel-selector",
                value={"expires_at": before.isoformat(), "delivered": False},
            ),
            AppState(
                key=pending_key_secondary,
                value={
                    "channel_instance_id": "telegram:secondary",
                    "selection_prompt": callback,
                    "created_at": before.isoformat(),
                    "selection_expires_at": before.isoformat(),
                },
            ),
        ]
    )
    db.commit()

    class Bot:
        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=1)

    asyncio.run(
        deliver(
            Bot(),
            db_engine,
            42,
            "selector-secondary",
            "Synthetic selection",
            keyboard={"inline_keyboard": [[{"text": "Choose", "callback_data": callback}]]},
            channel_instance=ChannelInstanceRef(channel="telegram", instance_id="secondary"),
        )
    )
    expiry = db.get(AppState, pending_key_secondary, populate_existing=True).value[
        "selection_expires_at"
    ]
    assert datetime.fromisoformat(expiry) > before + timedelta(minutes=15)


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


def test_colliding_update_ids_keep_provider_order(db):
    for number in (9952, 9953):
        _ingest(db, _update(number, "/status"), "primary")
        _ingest(db, _update(number, "/status"), "secondary")

    jobs = db.scalars(
        select(Job)
        .where(
            Job.kind == "telegram_control",
            cast(Job.payload["update_id"].astext, BigInteger) < 0,
        )
        .order_by(telegram_order())
    ).all()
    assert [job.payload["provider_update_id"] for job in jobs] == [9952, 9953]


def test_delayed_update_in_one_channel_does_not_block_another_channel(db):
    _ingest(db, _update(9955, "hello"), "primary")
    _ingest(db, _update(9956, "hello"), "secondary")
    delayed = db.scalar(select(Job).where(Job.payload["provider_update_id"].as_integer() == 9955))
    now = datetime.now(UTC) + timedelta(seconds=1)
    delayed.run_at = now + timedelta(hours=1)
    db.flush()

    claimed = claim(db, kinds=["telegram_update"], now=now)
    assert claimed is not None
    assert claimed.payload["channel_instance_id"] == "telegram:secondary"


def test_pause_controls_use_provider_order_after_instance_id_collisions(db, db_engine, monkeypatch):
    import garmin_ai.telegram as telegram_module

    fixed = datetime.now(UTC).replace(microsecond=0)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    for number, command in ((9961, "/pause"), (9962, "/resume")):
        _ingest(db, _update(number, "/status"), "primary")
        _ingest(db, _update(number, command), "secondary")
    secondary_ids = {
        row.payload["update_id"]: row.id
        for row in db.scalars(select(TelegramUpdate))
        if row.payload["_channel_instance"]["instance_id"] == "secondary"
    }
    monkeypatch.setattr(telegram_module, "datetime", FixedDatetime)
    for number in (9961, 9962):
        process_message(db_engine, None, _settings("secondary"), secondary_ids[number])

    state = db.get(AppState, "proactive:enabled", populate_existing=True).value
    assert state["enabled"] is True
    assert state["update_id"] == 9962


def test_standard_button_followup_binds_secondary_channel(db, db_engine):
    _ingest(db, _callback(9963, "medication"), "secondary")
    response = process_message(db_engine, None, _settings("secondary"), 9963)

    assert response
    db.info["channel_destination_instance_id"] = "telegram:secondary"
    pending = db.get(AppState, pending_key(db), populate_existing=True)
    assert pending.value["channel_instance_id"] == "telegram:secondary"


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
    db.info["channel_destination_instance_id"] = "telegram:secondary"
    assert context_for(db, datetime.now(UTC))["pending_clarification"] is None

    _ingest(db, _update(9962, "unrelated text"), "secondary")
    process_message(db_engine, None, _settings("secondary"), 9962)
    db.expire_all()
    assert db.get(AppState, "conversation:pending") is not None


def test_secondary_cancel_preserves_primary_pending_form(db, db_engine, sensitive_tracker):
    _grant(db, sensitive_tracker["tracker"]["definition_id"], "primary")
    _ingest(db, _callback(9978, sensitive_tracker["action"]["id"]), "primary")
    assert "Description" in process_message(db_engine, None, _settings("primary"), 9978)
    db.commit()
    primary = db.get(AppState, "conversation:pending", populate_existing=True)
    assert primary is not None

    _ingest(db, _update(9979, "/cancel"), "secondary")
    process_message(db_engine, None, _settings("secondary"), 9979)
    db.expire_all()
    assert db.get(AppState, "conversation:pending") is not None
    db.info["channel_destination_instance_id"] = "telegram:secondary"
    assert db.get(AppState, pending_key(db)) is None


def test_error_reply_keeps_empty_dependency_record(db, db_engine, sensitive_tracker, monkeypatch):
    from garmin_ai import telegram

    _ingest(db, _update(9980, "synthetic invalid request"), "secondary")
    monkeypatch.setattr(
        telegram, "_process_message", lambda *args: (_ for _ in ()).throw(ValueError())
    )
    process_message(db_engine, None, _settings("secondary"), 9980)
    db.expire_all()
    reply = db.get(AppState, "telegram:reply:9980")
    assert reply.value["share_requirements"] == {}
    assert reply.value["channel_instance_id"] == "telegram:secondary"
    assert telegram._reply_share_allowed(
        db, reply, ChannelInstanceRef(channel="telegram", instance_id="secondary")
    )


def test_model_definition_page_filters_each_historical_version(db, monkeypatch):
    from garmin_ai import definitions, share_policy
    from garmin_ai.tools import event_definitions

    current, historical = uuid4(), uuid4()
    row = {
        "key": "user.synthetic",
        "namespace": "user",
        "contract": {"id": str(current)},
        "versions": [{"id": str(current)}, {"id": str(historical)}],
    }
    monkeypatch.setattr(definitions, "list_definitions", lambda *args, **kwargs: [row])
    monkeypatch.setattr(
        share_policy,
        "version_sharing_allowed",
        lambda _session, version_id, **kwargs: version_id == current,
    )
    db.info.update(
        llm_access=True,
        model_provider_instance_id="model:gemini:primary",
        channel_destination_instance_id="telegram:secondary",
    )

    result = event_definitions(db)

    assert result["rows"][0]["versions"] == [{"id": str(current)}]
    assert set(db.info["channel_share_requirements"]) == {str(current)}


def test_channel_consent_fence_blocks_revoke_during_delivery(db_engine):
    from garmin_ai.share_policy import channel_consent_delivery_fence

    with channel_consent_delivery_fence(db_engine):
        with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as other:
            assert not other.scalar(text("SELECT pg_try_advisory_lock(72104631)"))


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
                    "analysis_epoch": "old",
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
    from garmin_ai.conversation import epoch_matches

    assert (
        db.get(AppState, "telegram:reply:2", populate_existing=True).value["analysis_epoch"]
        == "old"
    )
    assert epoch_matches(db, "old")
    db.info["channel_destination_instance_id"] = "telegram:primary"
    assert not epoch_matches(db, "old")


def test_reply_to_message_id_is_scoped_to_channel_instance(db):
    now = datetime.now(UTC)
    db.add(
        AppState(
            key="analysis:conversation",
            value={
                "epoch": "synthetic",
                "turns": [
                    {
                        "update_id": "551",
                        "channel_instance_id": "telegram:secondary",
                        "asked_at": now.isoformat(),
                        "question": "synthetic question",
                        "answer": "synthetic answer",
                    }
                ],
            },
        )
    )
    db.add_all(
        [
            AppState(
                key=f"outbox:update:{number}:0",
                value={
                    "status": "sent",
                    "message_id": 77,
                    "kind": kind,
                    "channel_instance_id": f"telegram:{instance}",
                },
            )
            for number, kind, instance in (
                (550, "diary", "primary"),
                (551, "analysis", "secondary"),
            )
        ]
    )
    db.flush()
    db.info["channel_destination_instance_id"] = "telegram:primary"
    assert not is_analytic_reply(db, 77)
    db.info["channel_destination_instance_id"] = "telegram:secondary"
    assert is_analytic_reply(db, 77)
    assert [turn["update_id"] for turn in conversation_context(db, now, 77)["turns"]] == ["551"]


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
