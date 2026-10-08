"""Run the public source/channel kit against a separately installed fixture."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.extension_tck import (
    ModelProbe,
    check_channel_adapter_sync,
    check_model_adapter,
    check_source_adapter,
)
from garmin_ai.integrations import IntegrationUnavailable, default_registry, integration_statuses
from garmin_ai.source_contracts import SourceCapabilities, SourcePage, SourceRecord


@pytest.fixture(autouse=True)
def installed_fixture():
    pytest.importorskip("synthetic_adapters")


def selected_settings():
    return Settings(
        integrations=[
            IntegrationInstance(
                id="source:sample:one", kind="source", provider="sample", config={"label": "one"}
            ),
            IntegrationInstance(
                id="channel:sample:one", kind="channel", provider="sample", config={"label": "one"}
            ),
        ]
    )


def test_installed_source_and_channel_pass_contract_kit():
    settings = selected_settings()
    registry = default_registry(settings)
    statuses = integration_statuses(settings, registry)
    assert {(status.kind, status.provider, status.available) for status in statuses} == {
        ("source", "sample", True),
        ("channel", "sample", True),
    }
    assert all(
        status.contract_version == 1
        and status.implementation_version == "0.0.1"
        and status.verification_level == "local_configuration"
        for status in statuses
    )
    assert all(
        status.verification_level == "declared"
        for status in integration_statuses(settings, registry, validate_runtime=False)
    )
    source = registry.create(settings.integrations[0], settings)
    channel = registry.create(settings.integrations[1], settings)
    assert check_source_adapter(source, instance_id="source:sample:one") == {
        "pages": 2,
        "records": 3,
        "instance_id": "source:sample:one",
    }
    assert check_channel_adapter_sync(channel, instance_id="channel:sample:one") == {
        "instance_id": "channel:sample:one",
        "state": "provider_accepted",
        "verified_capabilities": ["text"],
        "unverified_capabilities": ["initiatives"],
    }
    source.close()
    assert source.closed


def test_source_probe_rejects_mapping_page_even_if_schema_valid(monkeypatch):
    settings = selected_settings()
    source = default_registry(settings).create(settings.integrations[0], settings)
    original = source.read_page

    def mapping_page(*, start, end, cursor, limit):
        return original(start=start, end=end, cursor=cursor, limit=limit).model_dump(mode="python")

    monkeypatch.setattr(source, "read_page", mapping_page)
    with pytest.raises(AssertionError, match="must return a SourcePage"):
        check_source_adapter(source, instance_id="source:sample:one")


def test_installation_does_not_enable_plugin_and_instances_stay_separate():
    settings = selected_settings()
    assert integration_statuses(Settings(integrations=[])) == []
    disabled = settings.model_copy(
        update={"integrations": [settings.integrations[0].model_copy(update={"enabled": False})]}
    )
    assert integration_statuses(disabled)[0].reason == "integration is disabled"
    with pytest.raises(IntegrationUnavailable, match="disabled"):
        default_registry(disabled).create(disabled.integrations[0], disabled)

    second = settings.integrations[0].model_copy(
        update={"id": "source:sample:two", "config": {"label": "two"}}
    )
    together = settings.model_copy(update={"integrations": [settings.integrations[0], second]})
    registry = default_registry(together)
    one = registry.create(together.integrations[0], together)
    two = registry.create(second, together)
    assert one.instance_id != two.instance_id and one.label != two.label
    assert check_source_adapter(two, instance_id="source:sample:two")["records"] == 3
    one.close()
    assert one.closed and not two.closed


def test_runtime_starts_and_closes_selected_channel(db, db_engine, tmp_path, monkeypatch):
    import asyncio

    from synthetic_adapters import SampleChannel

    from garmin_ai import runtime

    db.commit()
    settings = Settings(
        integrations=[
            IntegrationInstance(
                id="channel:sample:one", kind="channel", provider="sample", config={"label": "one"}
            )
        ],
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _settings: db_engine)
    before = len(SampleChannel.instances)

    async def scenario():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "add_signal_handler",
            lambda _signal, cb: callbacks.append(cb),
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            for _ in range(100):
                if len(SampleChannel.instances) > before and callbacks:
                    break
                await asyncio.sleep(0.01)
            assert len(SampleChannel.instances) == before + 1
        finally:
            if callbacks:
                callbacks[0]()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    assert SampleChannel.instances[-1].closed


def test_channel_plugin_retries_transient_start_failure(db, db_engine, tmp_path, monkeypatch):
    import asyncio

    from synthetic_adapters import SampleChannel

    from garmin_ai import runtime
    from garmin_ai.accounts import owner
    from garmin_ai.channels import ChannelInstanceRef, OutboundIntent, TextBlock
    from garmin_ai.dialogue import queue_intent
    from garmin_ai.models import Conversation, OutboxMessage

    conversation_id = uuid4()
    db.add(
        Conversation(
            id=conversation_id,
            owner_id=owner(db).id,
            channel="sample",
            channel_instance_id="one",
            external_conversation_id="fictional-chat",
            memory_epoch=uuid4(),
            state={},
        )
    )
    db.flush()
    message = queue_intent(
        db,
        OutboundIntent(
            owner_id=owner(db).id,
            conversation_id=conversation_id,
            channel_instance=ChannelInstanceRef(channel="sample", instance_id="one"),
            blocks=[TextBlock(text="Fictional reminder")],
            initiative=True,
        ),
        operation_id=uuid4(),
        dedup_key="fictional-retry-start",
    )
    message_id = message.id
    db.commit()
    settings = Settings(
        integrations=[selected_settings().integrations[1]],
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
    )
    registry = default_registry(settings)
    original_create = registry.create
    attempts = []

    def fail_first_start(instance, active_settings):
        if instance.kind == "channel":
            attempts.append(instance.id)
            if len(attempts) == 1:
                raise RuntimeError("fictional temporary startup failure")
        return original_create(instance, active_settings)

    monkeypatch.setattr(registry, "create", fail_first_start)
    monkeypatch.setattr(runtime, "default_registry", lambda _settings: registry)
    monkeypatch.setattr(runtime, "make_engine", lambda _settings: db_engine)
    before = len(SampleChannel.instances)

    async def scenario():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "add_signal_handler",
            lambda _signal, callback: callbacks.append(callback),
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            for _ in range(200):
                await asyncio.sleep(0.05)
                db.expire_all()
                if db.get(OutboxMessage, message_id).state == "provider_accepted":
                    break
            else:
                pytest.fail("Channel plugin did not retry after startup failure")
            assert attempts == ["channel:sample:one", "channel:sample:one"]
            assert len(SampleChannel.instances) == before + 1
        finally:
            if callbacks:
                callbacks[0]()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    assert SampleChannel.instances[-1].closed


def test_plugin_initiative_runs_while_telegram_startup_is_unavailable(
    db, db_engine, tmp_path, monkeypatch
):
    import asyncio

    from sqlalchemy import select
    from synthetic_adapters import SampleChannel

    from garmin_ai import initiative_rules, runtime
    from garmin_ai.accounts import owner
    from garmin_ai.channels import ChannelInstanceRef, OutboundIntent, TextBlock
    from garmin_ai.dialogue import queue_intent
    from garmin_ai.models import Conversation, Job, OutboxMessage

    conversation_id = uuid4()
    db.add(
        Conversation(
            id=conversation_id,
            owner_id=owner(db).id,
            channel="sample",
            channel_instance_id="one",
            external_conversation_id="fictional-chat",
            memory_epoch=uuid4(),
            state={},
        )
    )
    db.flush()
    intent = OutboundIntent(
        owner_id=owner(db).id,
        conversation_id=conversation_id,
        channel_instance=ChannelInstanceRef(channel="sample", instance_id="one"),
        blocks=[TextBlock(text="Fictional reminder")],
        initiative=True,
    )
    telegram_conversation_id = uuid4()
    db.add(
        Conversation(
            id=telegram_conversation_id,
            owner_id=owner(db).id,
            channel="telegram",
            channel_instance_id="primary",
            external_conversation_id="fictional-telegram-chat",
            memory_epoch=uuid4(),
            state={},
        )
    )
    db.flush()
    telegram_message = queue_intent(
        db,
        intent.model_copy(
            update={
                "intent_id": uuid4(),
                "conversation_id": telegram_conversation_id,
                "channel_instance": ChannelInstanceRef(channel="telegram", instance_id="primary"),
            }
        ),
        operation_id=uuid4(),
        dedup_key="fictional-telegram-alert",
    )
    telegram_message_id = telegram_message.id
    db.commit()

    settings = Settings(
        integrations=[
            IntegrationInstance(id="channel:telegram:primary", kind="channel", provider="telegram"),
            selected_settings().integrations[1],
        ],
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="synthetic",
        telegram_user_id=42,
        llm_enabled=False,
    )

    class UnavailableBot:
        def __init__(self, *_args, **_kwargs):
            pass

        async def initialize(self):
            raise RuntimeError("synthetic Telegram startup failure")

        async def shutdown(self):
            pass

    monkeypatch.setattr(runtime, "make_engine", lambda _settings: db_engine)
    monkeypatch.setattr(runtime, "Bot", UnavailableBot)
    generated = []

    def generate_fictional_tracker_reminder(session, _settings, _now):
        if not generated:
            message = queue_intent(
                session, intent, operation_id=uuid4(), dedup_key="fictional-plugin-alert"
            )
            generated.append(message.id)
        return []

    monkeypatch.setattr(
        initiative_rules, "queue_due_tracker_checkins", generate_fictional_tracker_reminder
    )
    before = len(SampleChannel.instances)

    async def scenario():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "add_signal_handler",
            lambda _signal, callback: callbacks.append(callback),
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            for _ in range(200):
                await asyncio.sleep(0.05)
                db.expire_all()
                if generated and db.get(OutboxMessage, generated[0]).state == "provider_accepted":
                    break
            else:
                pytest.fail("Plugin tracker initiative stayed blocked by Telegram startup")
            assert len(generated) == 1
            assert len(SampleChannel.instances) == before + 1
            assert db.scalars(select(Job).where(Job.kind == "channel_initiatives")).all()
            assert db.get(OutboxMessage, telegram_message_id).state == "queued"
            assert not task.done()
        finally:
            if callbacks:
                callbacks[0]()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())


def test_channel_selection_change_activates_configured_plugin_without_restart(
    db, db_engine, tmp_path, monkeypatch
):
    import asyncio
    from datetime import UTC, datetime

    from synthetic_adapters import SampleChannel

    from garmin_ai import runtime
    from garmin_ai.accounts import owner
    from garmin_ai.channels import ChannelInstanceRef, OutboundIntent, TextBlock
    from garmin_ai.db import transaction
    from garmin_ai.dialogue import queue_intent
    from garmin_ai.jobs import enqueue
    from garmin_ai.models import AppState, Conversation, Job, OutboxMessage

    conversation_id = uuid4()
    db.add(
        Conversation(
            id=conversation_id,
            owner_id=owner(db).id,
            channel="sample",
            channel_instance_id="one",
            external_conversation_id="fictional-chat",
            memory_epoch=uuid4(),
            state={},
        )
    )
    db.add(
        AppState(
            key="preferences:onboarding",
            value={"channel": None, "fallback_channels": [], "source_instance_ids": []},
        )
    )
    db.flush()
    message = queue_intent(
        db,
        OutboundIntent(
            owner_id=owner(db).id,
            conversation_id=conversation_id,
            channel_instance=ChannelInstanceRef(channel="sample", instance_id="one"),
            blocks=[TextBlock(text="Fictional reminder")],
            initiative=True,
        ),
        operation_id=uuid4(),
        dedup_key="fictional-after-selection",
    )
    message_id = message.id
    db.commit()
    settings = Settings(
        integrations=[selected_settings().integrations[1]],
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _settings: db_engine)
    before = len(SampleChannel.instances)

    async def scenario():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "add_signal_handler",
            lambda _signal, callback: callbacks.append(callback),
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                db.expire_all()
                jobs = db.query(Job).filter(Job.kind == "channel_initiatives").all()
                if jobs and jobs[0].status == "done":
                    break
            else:
                pytest.fail("Initial channel job was not processed")
            assert len(SampleChannel.instances) == before + 1
            assert db.get(OutboxMessage, message_id).state == "queued"
            db.rollback()
            with transaction(db_engine) as session:
                saved = session.get(AppState, "preferences:onboarding")
                saved.value = {
                    **saved.value,
                    "channel": {"channel": "sample", "instance_id": "one"},
                }
                enqueue(
                    session,
                    "channel_initiatives",
                    {},
                    "fictional-selection-change",
                    datetime.now(UTC),
                )
            for _ in range(100):
                await asyncio.sleep(0.05)
                db.expire_all()
                if db.get(OutboxMessage, message_id).state == "provider_accepted":
                    break
            else:
                pytest.fail("Selected plugin did not deliver without restart")
            assert not task.done()
        finally:
            if callbacks:
                callbacks[0]()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())


def test_runtime_rejects_duplicate_derived_channel_destinations(
    db, db_engine, tmp_path, monkeypatch
):
    import asyncio

    from garmin_ai import runtime

    db.commit()
    settings = Settings(
        integrations=[
            IntegrationInstance(
                id="channel:sample:one",
                kind="channel",
                provider="sample",
                config={"label": "first"},
            ),
            IntegrationInstance(
                id="one", kind="channel", provider="sample", config={"label": "second"}
            ),
        ],
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _settings: db_engine)
    with pytest.raises(ValueError, match="share a delivery destination"):
        asyncio.run(runtime.run(settings))


def test_fallback_telegram_does_not_receive_primary_insight_notices(
    db, db_engine, tmp_path, monkeypatch
):
    import asyncio
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from garmin_ai import runtime
    from garmin_ai.jobs import enqueue
    from garmin_ai.models import AppState, Job
    from garmin_ai.storage_alerts import KEY as STORAGE_KEY

    db.add(
        AppState(
            key="preferences:onboarding",
            value={
                "channel": {"channel": "sample", "instance_id": "one"},
                "fallback_channels": [{"channel": "telegram", "instance_id": "primary"}],
                "source_instance_ids": [],
                "model_categories": ["diary"],
            },
        )
    )
    now = datetime.now(UTC)
    job_id = enqueue(db, "agent_insights", {}, "fictional-primary-insight", now)
    proactive_id = enqueue(db, "agent_proactive", {}, "fictional-primary-question", now)
    db.add(AppState(key=STORAGE_KEY, value={"status": "insufficient"}))
    notice_ids = [
        enqueue(
            db,
            "telegram_connection_notice",
            {"category": "auth", "key": "fictional-connection-notice"},
            "fictional-connection-notice",
            now,
        ),
        enqueue(
            db,
            "telegram_storage_notice",
            {"day": now.date().isoformat()},
            "fictional-storage-notice",
            now,
        ),
        enqueue(
            db,
            "telegram_provider_notice",
            {"outbox_key": "fictional-provider-notice"},
            "fictional-provider-notice",
            now,
        ),
    ]
    db.commit()
    settings = Settings(
        integrations=[
            IntegrationInstance(id="channel:telegram:primary", kind="channel", provider="telegram"),
            selected_settings().integrations[1],
            IntegrationInstance(id="model:gemini:primary", kind="model", provider="gemini"),
        ],
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="synthetic",
        telegram_user_id=42,
        llm_enabled=False,
    )
    calls = []
    selected_questions = []
    ready = asyncio.Event()

    class Bot:
        def __init__(self, *_args, **_kwargs):
            pass

        async def initialize(self):
            pass

        async def get_webhook_info(self):
            return SimpleNamespace(url="")

        async def shutdown(self):
            pass

        async def send_message(self, **_kwargs):
            calls.append("telegram")
            return SimpleNamespace(message_id=1)

    async def fake_poll(_bot, _engine, _settings, stop, notifications_ready, **_kwargs):
        notifications_ready.set()
        ready.set()
        await stop.wait()

    async def fake_deliver(*_args, **_kwargs):
        calls.append("telegram")

    monkeypatch.setattr(runtime, "make_engine", lambda _settings: db_engine)
    monkeypatch.setattr(
        runtime, "create_model_provider", lambda *_args: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(runtime, "Bot", Bot)
    monkeypatch.setattr(runtime, "poll", fake_poll)
    monkeypatch.setattr(runtime, "generate_insights", lambda *_args: None)
    monkeypatch.setattr(runtime, "generate_questions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime, "select_question", lambda *_args, **_kwargs: selected_questions.append("called")
    )
    monkeypatch.setattr(
        runtime,
        "pending_insight_notices",
        lambda *_args: [SimpleNamespace(id=uuid4())],
    )
    monkeypatch.setattr(runtime, "can_notify", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(runtime, "deliver_current_insight", fake_deliver)

    async def scenario():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "add_signal_handler",
            lambda _signal, callback: callbacks.append(callback),
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            for _ in range(100):
                await asyncio.sleep(0.05)
                db.expire_all()
                if (
                    db.get(Job, job_id).status == "done"
                    and db.get(Job, proactive_id).status == "done"
                    and all(db.get(Job, notice_id).status == "done" for notice_id in notice_ids)
                ):
                    break
            else:
                pytest.fail("Primary-only notification jobs were not processed")
            assert calls == []
            assert selected_questions == []
            assert not task.done()
        finally:
            if callbacks:
                callbacks[0]()
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())


def test_direct_telegram_notice_requires_primary_channel(db, db_engine):
    import asyncio

    from garmin_ai import runtime
    from garmin_ai.channels import ChannelInstanceRef
    from garmin_ai.models import AppState

    selected = ChannelInstanceRef(channel="telegram", instance_id="primary")
    state = AppState(
        key="preferences:onboarding",
        value={"channel": {"channel": "telegram", "instance_id": "primary"}},
    )
    db.add(state)
    db.commit()
    calls = []

    async def send():
        calls.append("sent")

    assert asyncio.run(runtime.deliver_primary_telegram_notice(db_engine, selected, send))
    db.refresh(state)
    state.value = {"channel": {"channel": "sample", "instance_id": "one"}}
    db.commit()
    assert not asyncio.run(runtime.deliver_primary_telegram_notice(db_engine, selected, send))
    assert calls == ["sent"]


def test_runtime_delivers_neutral_initiative_through_selected_channel(db, db_engine, monkeypatch):
    import asyncio
    from contextlib import nullcontext

    from garmin_ai import dialogue, initiative_rules, runtime, share_policy
    from garmin_ai.channels import ChannelInstanceRef, OutboundIntent, TextBlock
    from garmin_ai.initiative_rules import InitiativeLease

    settings = selected_settings()
    adapter = default_registry(settings).create(settings.integrations[1], settings)
    intent = OutboundIntent(
        owner_id=uuid4(),
        conversation_id=uuid4(),
        channel_instance=ChannelInstanceRef(channel="sample", instance_id="one"),
        blocks=[TextBlock(text="Fictional reminder")],
        initiative=True,
    )
    lease = InitiativeLease(outbox_message_id=uuid4(), lease_token=uuid4(), intent=intent)
    pending = [lease]
    completed = []
    monkeypatch.setattr(
        initiative_rules,
        "claim_due_initiative",
        lambda *_args, **_kw: pending.pop(0) if pending else None,
    )
    monkeypatch.setattr(
        initiative_rules,
        "finish_initiative_attempt",
        lambda _db, _lease, attempt, _now: completed.append(attempt),
    )
    monkeypatch.setattr(dialogue, "recover_expired_outbox_leases", lambda *_args: None)
    monkeypatch.setattr(runtime, "initiative_delivery_fence", lambda _engine: nullcontext())
    monkeypatch.setattr(
        share_policy, "channel_consent_delivery_fence", lambda _engine: nullcontext()
    )

    asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {"sample:one": lambda: adapter}))

    assert len(adapter.deliveries) == 1
    assert adapter.deliveries[0].intent_id == intent.intent_id
    assert len(completed) == 1 and completed[0].intent_id == intent.intent_id
    assert completed[0].state.value == "provider_accepted"

    original_deliver = adapter.deliver

    async def stale_attempt(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(update={"intent_id": uuid4()})

    monkeypatch.setattr(adapter, "deliver", stale_attempt)
    pending.append(lease)
    asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {"sample:one": lambda: adapter}))
    assert completed[-1].state.value == "uncertain"

    async def accepted_without_receipt(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(update={"receipt": None})

    monkeypatch.setattr(adapter, "deliver", accepted_without_receipt)
    pending.append(lease)
    asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {"sample:one": lambda: adapter}))
    assert completed[-1].state.value == "uncertain"

    async def accepted_without_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(update={"rendered": None})

    monkeypatch.setattr(adapter, "deliver", accepted_without_render)
    pending.append(lease)
    asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {"sample:one": lambda: adapter}))
    assert completed[-1].state.value == "uncertain"

    from garmin_ai.channels import DeliveryState

    async def queued_with_accepted_receipt(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(update={"state": DeliveryState.QUEUED})

    monkeypatch.setattr(adapter, "deliver", queued_with_accepted_receipt)
    pending.append(lease)
    asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {"sample:one": lambda: adapter}))
    assert completed[-1].state.value == "uncertain"

    async def failed_with_queued_receipt(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={
                "state": DeliveryState.FAILED,
                "receipt": result.receipt.model_copy(update={"state": DeliveryState.QUEUED}),
            }
        )

    monkeypatch.setattr(adapter, "deliver", failed_with_queued_receipt)
    pending.append(lease)
    asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {"sample:one": lambda: adapter}))
    assert completed[-1].state.value == "uncertain"

    async def accepted_with_delivered_receipt(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={
                "receipt": result.receipt.model_copy(update={"state": DeliveryState.DELIVERED}),
            }
        )

    monkeypatch.setattr(adapter, "deliver", accepted_with_delivered_receipt)
    pending.append(lease)
    asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {"sample:one": lambda: adapter}))
    assert completed[-1].state.value == "provider_accepted"
    assert completed[-1].receipt.state.value == "delivered"

    async def wrong_attempt_type(intent, *, now):
        return (await original_deliver(intent, now=now)).model_dump(mode="python")

    monkeypatch.setattr(adapter, "deliver", wrong_attempt_type)
    pending.append(lease)
    asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {"sample:one": lambda: adapter}))
    assert completed[-1].state.value == "uncertain"


def test_initiative_claim_refreshes_time_after_delivery_fences(db_engine, monkeypatch):
    import asyncio
    from datetime import UTC, datetime, timedelta

    from garmin_ai import dialogue, initiative_rules, runtime

    before = datetime(2026, 1, 1, tzinfo=UTC)
    after = before + timedelta(minutes=2)
    observed = []

    class AdvancingClock:
        calls = 0

        @classmethod
        def now(cls, _timezone):
            cls.calls += 1
            return before if cls.calls == 1 else after

    monkeypatch.setattr(runtime, "datetime", AdvancingClock)
    monkeypatch.setattr(dialogue, "recover_expired_outbox_leases", lambda *_args: None)
    monkeypatch.setattr(
        initiative_rules,
        "claim_due_initiative",
        lambda _session, now, **_kwargs: observed.append(now) or None,
    )

    asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {}))

    assert observed == [after]


def test_source_contract_rejects_duplicate_records_and_payload_on_deletion():
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    record = SourceRecord(
        source_record_id="one",
        observed_at=now,
        effective_at=now,
        source_timezone="UTC",
        source_reference="sample:one",
        payload={"value": 1},
    )
    with pytest.raises(ValidationError, match="repeat a record identity"):
        SourcePage(
            instance_id="source:sample:one",
            page_kind="partial",
            fetched_at=now,
            records=[record, record],
        )
    with pytest.raises(ValidationError, match="cannot carry"):
        SourceRecord.model_validate(
            {**record.model_dump(mode="json"), "operation": "delete", "payload": {"value": 1}}
        )
    with pytest.raises(ValidationError, match="Effective end"):
        SourceRecord.model_validate(
            {**record.model_dump(mode="json"), "effective_end": now.isoformat()}
        )


def test_source_payload_has_bounded_json_shape_and_size():
    from datetime import UTC, datetime

    base = {
        "source_record_id": "one",
        "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
        "source_timezone": "UTC",
        "source_reference": "synthetic:one",
    }
    assert len(SourceRecord(**base, payload={f"key_{i}": i for i in range(256)}).payload) == 256
    for payload, reason in (
        ({"text": "x" * 17_000}, "byte limit"),
        ({"nested": [[[[[[[[[1]]]]]]]]]}, "nesting limit"),
        ({"values": list(range(257))}, "collection limit"),
        ({"value": float("nan")}, "finite JSON values"),
        ({"value": {1, 2}}, "finite JSON values"),
    ):
        with pytest.raises(ValidationError, match=reason):
            SourceRecord(**base, payload=payload)


def test_source_probe_accepts_more_than_ten_pages_and_bounds_intervals():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 1, 2, tzinfo=UTC)

    class DenseSource:
        capabilities = SourceCapabilities(cursor=True, max_page_size=2, time_semantics="interval")

        def close(self):
            pass

        def read_page(self, *, start, end, cursor, limit):
            offset = int(cursor or "0")
            records = [
                SourceRecord(
                    source_record_id=f"row-{i}",
                    observed_at=now,
                    effective_at=now - timedelta(hours=1),
                    effective_end=now - timedelta(minutes=30),
                    source_timezone="UTC",
                    source_reference=f"synthetic:{i}",
                )
                for i in range(offset, min(offset + limit, 25))
            ]
            return SourcePage(
                instance_id="source:synthetic:one",
                page_kind="partial",
                fetched_at=now,
                records=records,
                next_cursor=str(offset + len(records)) if offset + len(records) < 25 else None,
            )

    assert check_source_adapter(DenseSource(), instance_id="source:synthetic:one") == {
        "pages": 13,
        "records": 25,
        "instance_id": "source:synthetic:one",
    }

    class OverlappingPages(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            if cursor == "2":
                previous = super().read_page(start=start, end=end, cursor=None, limit=limit)
                page.records = [previous.records[-1], page.records[0]]
                page.next_cursor = "3"
            return page

    assert check_source_adapter(OverlappingPages(), instance_id="source:synthetic:one") == {
        "pages": 13,
        "records": 25,
        "instance_id": "source:synthetic:one",
    }
    with pytest.raises(AssertionError, match="page budget"):
        check_source_adapter(DenseSource(), instance_id="source:synthetic:one", max_pages=10)

    class MissingEnd(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            page.records[0].effective_end = None
            return page

    with pytest.raises(AssertionError, match="effective end"):
        check_source_adapter(MissingEnd(), instance_id="source:synthetic:one")

    class MutatedPayload(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            page.records[0].payload["text"] = "x" * 17_000
            return page

    with pytest.raises(ValidationError, match="byte limit"):
        check_source_adapter(MutatedPayload(), instance_id="source:synthetic:one")

    class FinalPageSnapshot(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            if page.next_cursor is None:
                page.page_kind = "complete_interval_snapshot"
            return page

    with pytest.raises(AssertionError, match="cannot follow a pagination cursor"):
        check_source_adapter(FinalPageSnapshot(), instance_id="source:synthetic:one")

    class Overnight(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            if cursor is None:
                page.records[0].effective_at = start - timedelta(minutes=30)
                page.records[0].effective_end = start + timedelta(minutes=30)
            return page

    assert check_source_adapter(Overnight(), instance_id="source:synthetic:one")["records"] == 25

    class Outside(Overnight):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            if cursor is None:
                page.records[0].effective_end = start
            return page

    with pytest.raises(AssertionError, match="outside the requested window"):
        check_source_adapter(Outside(), instance_id="source:synthetic:one")

    class MissingClose(DenseSource):
        close = None

    with pytest.raises(AssertionError, match="close lifecycle"):
        check_source_adapter(MissingClose(), instance_id="source:synthetic:one")

    class EmptySource(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            return SourcePage(
                instance_id="source:synthetic:one",
                page_kind="partial",
                fetched_at=now,
            )

    with pytest.raises(AssertionError, match="at least one synthetic record"):
        check_source_adapter(EmptySource(), instance_id="source:synthetic:one")

    class InvalidCapabilities(DenseSource):
        capabilities = SourceCapabilities.model_construct(
            observations=True, cursor=True, max_page_size=2, time_semantics="unsupported"
        )

    with pytest.raises(ValidationError, match="time_semantics"):
        check_source_adapter(InvalidCapabilities(), instance_id="source:synthetic:one")


def test_calendar_day_probe_uses_source_local_day_overlap():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 1, 2, tzinfo=UTC)
    zone = ZoneInfo("Pacific/Kiritimati")

    class DailySource:
        capabilities = SourceCapabilities(time_semantics="calendar_day")

        def __init__(self, local_day):
            self.local_day = local_day

        def close(self):
            pass

        def read_page(self, *, start, end, cursor, limit):
            return SourcePage(
                instance_id="source:daily:one",
                page_kind="partial",
                fetched_at=now,
                records=[
                    SourceRecord(
                        source_record_id="day-1",
                        observed_at=now,
                        effective_at=self.local_day,
                        source_timezone="Pacific/Kiritimati",
                        source_reference="synthetic:day-1",
                    )
                ],
            )

    overlapping = DailySource(datetime(2026, 1, 1, tzinfo=zone))
    assert check_source_adapter(overlapping, instance_id="source:daily:one")["records"] == 1
    outside = DailySource(datetime(2025, 12, 31, tzinfo=zone))
    with pytest.raises(AssertionError, match="outside the requested window"):
        check_source_adapter(outside, instance_id="source:daily:one")


def test_channel_probe_uses_provider_and_rejects_foreign_receipt(monkeypatch):
    settings = selected_settings()
    channel = default_registry(settings).create(settings.integrations[1], settings)
    original_policy = channel.delivery_policy
    original_deliver = channel.deliver

    def policy(intent, *, now):
        assert intent.channel_instance.channel == "alternate"
        assert intent.channel_instance.instance_id == "one"
        return original_policy(intent, now=now)

    monkeypatch.setattr(channel, "delivery_policy", policy)
    assert (
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")["state"]
        == "provider_accepted"
    )

    async def stale(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"receipt": result.receipt.model_copy(update={"intent_id": uuid4()})}
        )

    monkeypatch.setattr(channel, "deliver", stale)
    with pytest.raises(AssertionError, match="matching observed receipt"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    async def mapping_attempt(intent, *, now):
        return (await original_deliver(intent, now=now)).model_dump(mode="python")

    monkeypatch.setattr(channel, "deliver", mapping_attempt)
    with pytest.raises(AssertionError, match="must return a DeliveryAttempt"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    from garmin_ai.channels import DeliveryState

    async def delivered(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={
                "state": DeliveryState.DELIVERED,
                "receipt": result.receipt.model_copy(update={"state": DeliveryState.READ}),
            }
        )

    monkeypatch.setattr(channel, "deliver", delivered)
    assert check_channel_adapter_sync(channel, instance_id="channel:alternate:one")["state"] == (
        "delivered"
    )

    async def stale_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"rendered": result.rendered.model_copy(update={"intent_id": uuid4()})}
        )

    monkeypatch.setattr(channel, "deliver", stale_render)
    with pytest.raises(AssertionError, match="Rendered delivery"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    async def oversized_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"rendered": result.rendered.model_copy(update={"texts": ["x" * 201]})}
        )

    monkeypatch.setattr(channel, "deliver", oversized_render)
    with pytest.raises(AssertionError, match="declared channel limit"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    async def dropped_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"rendered": result.rendered.model_copy(update={"texts": []})}
        )

    monkeypatch.setattr(channel, "deliver", dropped_render)
    with pytest.raises(AssertionError, match="omitted the probe text"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    from garmin_ai.channels import (
        ActionRef,
        AttachmentRef,
        ChannelInstanceRef,
        ExternalMessageRef,
    )

    for update, reason in (
        ({"medium": "voice"}, "undeclared voice"),
        (
            {"actions": [ActionRef(action_id="one", label="One", operation_id=uuid4())]},
            "undeclared action",
        ),
        ({"attachments": [AttachmentRef(kind="image")]}, "undeclared attachment"),
        ({"mode": "edit"}, "undeclared edit"),
        (
            {
                "reply_to": ExternalMessageRef(
                    channel_instance=ChannelInstanceRef(channel="alternate", instance_id="one"),
                    external_message_id="fictional",
                )
            },
            "undeclared reply",
        ),
    ):

        async def unsupported_render(intent, *, now, changes=update):
            result = await original_deliver(intent, now=now)
            return result.model_copy(
                update={"rendered": result.rendered.model_copy(update=changes)}
            )

        monkeypatch.setattr(channel, "deliver", unsupported_render)
        with pytest.raises(AssertionError, match=reason):
            check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    async def malformed_receipt(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"receipt": result.receipt.model_copy(update={"observed_at": "not-a-date"})}
        )

    monkeypatch.setattr(channel, "deliver", malformed_receipt)
    with pytest.raises(ValidationError, match="observed_at"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    from garmin_ai.channels import ChannelCapabilities, DeliveryPolicy

    channel._capabilities = ChannelCapabilities.model_construct(
        text=True, max_text_length=1_000_001
    )
    with pytest.raises(ValidationError, match="max_text_length"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")
    channel._capabilities = ChannelCapabilities(text=True, max_text_length=200)

    monkeypatch.setattr(
        channel,
        "delivery_policy",
        lambda _intent, *, now: DeliveryPolicy.model_construct(
            allow_delivery=True, retry_after="not-a-date"
        ),
    )
    with pytest.raises(ValidationError, match="retry_after"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")


def test_channel_probe_accepts_configured_synthetic_recipient(monkeypatch):
    from garmin_ai.channels import DeliveryPolicy

    settings = selected_settings()
    channel = default_registry(settings).create(settings.integrations[1], settings)
    expected_owner = uuid4()
    expected_conversation = uuid4()
    original_policy = channel.delivery_policy

    def bound_policy(intent, *, now):
        if intent.owner_id != expected_owner or intent.conversation_id != expected_conversation:
            return DeliveryPolicy(allow_delivery=False)
        return original_policy(intent, now=now)

    monkeypatch.setattr(channel, "delivery_policy", bound_policy)
    with pytest.raises(AssertionError, match="rejected its own text probe"):
        check_channel_adapter_sync(channel, instance_id="channel:sample:one")
    assert (
        check_channel_adapter_sync(
            channel,
            instance_id="channel:sample:one",
            owner_id=expected_owner,
            conversation_id=expected_conversation,
        )["state"]
        == "provider_accepted"
    )


def test_channel_probe_reports_optional_capabilities_as_unverified():
    from garmin_ai.channels import ChannelCapabilities, InMemoryChannel

    channel = InMemoryChannel(
        ChannelCapabilities(
            text=True,
            actions=True,
            attachments=True,
            voice=True,
            edit=True,
            reply=True,
            initiatives=True,
        )
    )
    result = check_channel_adapter_sync(channel, instance_id="channel:sample:one")
    assert result["verified_capabilities"] == ["text"]
    assert result["unverified_capabilities"] == [
        "actions",
        "attachments",
        "voice",
        "edit",
        "reply",
        "initiatives",
    ]


def test_channel_text_probe_rejects_advertised_voice_render(monkeypatch):
    from garmin_ai.channels import ChannelCapabilities, InMemoryChannel

    channel = InMemoryChannel(ChannelCapabilities(text=True, voice=True))
    original_deliver = channel.deliver

    async def voice_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"rendered": result.rendered.model_copy(update={"medium": "voice"})}
        )

    monkeypatch.setattr(channel, "deliver", voice_render)
    with pytest.raises(AssertionError, match="must render as text"):
        check_channel_adapter_sync(channel, instance_id="channel:sample:one")

    async def no_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(update={"rendered": None})

    monkeypatch.setattr(channel, "deliver", no_render)
    with pytest.raises(AssertionError, match="requires a rendered delivery"):
        check_channel_adapter_sync(channel, instance_id="channel:sample:one")


def test_channel_text_probe_rejects_unrequested_optional_rendering(monkeypatch):
    from garmin_ai.channels import (
        ActionRef,
        AttachmentRef,
        ChannelCapabilities,
        ChannelInstanceRef,
        ExternalMessageRef,
        InMemoryChannel,
    )

    channel = InMemoryChannel(
        ChannelCapabilities(text=True, actions=True, attachments=True, edit=True, reply=True)
    )
    original_deliver = channel.deliver
    reference = ExternalMessageRef(
        channel_instance=ChannelInstanceRef(channel="sample", instance_id="one"),
        external_message_id="fictional",
    )
    for changes, reason in (
        ({"mode": "edit"}, "must send a new message"),
        (
            {"actions": [ActionRef(action_id="one", label="One", operation_id=uuid4())]},
            "unrequested",
        ),
        ({"attachments": [AttachmentRef(kind="image")]}, "unrequested"),
        ({"reply_to": reference}, "unrequested"),
        ({"related_to": reference}, "unrequested"),
    ):

        async def extra_render(intent, *, now, update=changes):
            result = await original_deliver(intent, now=now)
            return result.model_copy(update={"rendered": result.rendered.model_copy(update=update)})

        monkeypatch.setattr(channel, "deliver", extra_render)
        with pytest.raises(AssertionError, match=reason):
            check_channel_adapter_sync(channel, instance_id="channel:sample:one")


def test_declared_status_does_not_require_local_plugin_config_or_secrets(monkeypatch):
    settings = selected_settings()
    source = settings.integrations[0].model_copy(
        update={"config": {}, "secret_refs": {"example": "GA_MISSING_TEST_SECRET"}}
    )
    monkeypatch.delenv("GA_MISSING_TEST_SECRET", raising=False)
    registry = default_registry(settings)
    assert (
        registry.status(source, settings, validate_runtime=False).verification_level == "declared"
    )
    invalid = registry.status(source, settings, validate_runtime=True)
    assert invalid.reason == "invalid integration configuration"
    assert (invalid.contract_version, invalid.implementation_version) == (1, "0.0.1")
    missing_secret = source.model_copy(update={"config": {"label": "one"}})
    secret_status = registry.status(missing_secret, settings, validate_runtime=True)
    assert secret_status.reason == "missing secret reference: example"
    assert (secret_status.contract_version, secret_status.implementation_version) == (1, "0.0.1")


def test_model_probe_closes_adapter_after_failure():
    class BrokenModel:
        closed = False

        def structured(self, *_args):
            raise RuntimeError("synthetic failure")

        def close(self):
            self.closed = True

    model = BrokenModel()
    with pytest.raises(RuntimeError, match="synthetic failure"):
        check_model_adapter(model)
    assert model.closed


def test_model_probe_accepts_any_schema_valid_boolean():
    class ValidModel:
        closed = False

        def structured(self, *_args):
            return ModelProbe(urgent=True)

        def close(self):
            self.closed = True

    model = ValidModel()
    assert check_model_adapter(model) == {"structured_output": True}
    assert model.closed

    class InvalidModel(ValidModel):
        def structured(self, *_args):
            return ModelProbe.model_construct(urgent=[])

    invalid = InvalidModel()
    with pytest.raises(ValidationError, match="urgent"):
        check_model_adapter(invalid)
    assert invalid.closed
