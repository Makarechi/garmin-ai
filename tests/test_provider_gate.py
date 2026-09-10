from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from garmin_ai.config import Settings
from garmin_ai.llm import (
    GeminiProvider,
    ProviderAuthError,
    ProviderCooldown,
    ProviderModelUnavailable,
    ProviderRateLimited,
    ProviderUnavailable,
)
from garmin_ai.models import AppState
from garmin_ai.provider_gate import KEY, LOCK, ProviderGate

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def settings():
    return Settings(gemini_model="synthetic-model", gemini_api_key="synthetic-key")


@pytest.mark.parametrize(
    "error,reason",
    [
        (ProviderRateLimited, "quota"),
        (ProviderAuthError, "auth"),
        (ProviderModelUnavailable, "model"),
        (ProviderUnavailable, "unavailable"),
    ],
)
def test_failure_pauses_other_requests_and_survives_new_gate(db, db_engine, error, reason):
    calls = []

    def unavailable(**kwargs):
        calls.append(True)
        raise error("synthetic-private-provider-detail")

    gate = ProviderGate(db_engine, settings(), lambda: NOW)
    with pytest.raises(error):
        gate.call(unavailable)
    restarted = ProviderGate(db_engine, settings(), lambda: NOW + timedelta(seconds=1))
    for _ in range(3):
        with pytest.raises(ProviderCooldown) as paused:
            restarted.call(unavailable)
        assert paused.value.reason == reason and paused.value.retry_seconds > 0
    assert len(calls) == 1
    db.expire_all()
    assert "synthetic-private-provider-detail" not in str(db.get(AppState, KEY).value)
    assert "synthetic-key" not in str(db.get(AppState, KEY).value)


def test_expired_pause_retries_and_success_clears_gate(db, db_engine):
    gate = ProviderGate(db_engine, settings(), lambda: NOW)
    gate.record("quota", NOW + timedelta(seconds=1))
    restarted = ProviderGate(db_engine, settings(), lambda: NOW + timedelta(seconds=2))
    assert restarted.call(lambda: "synthetic") == "synthetic"
    db.expire_all()
    assert db.get(AppState, KEY).value["reason"] == "ready"


def test_changed_configuration_can_recover_authorization(db_engine):
    config = settings()
    gate = ProviderGate(db_engine, config, lambda: NOW)
    gate.record("auth", NOW + timedelta(hours=1))
    config.gemini_api_key = SecretStr("synthetic-replacement-key")
    assert ProviderGate(db_engine, config, lambda: NOW).call(lambda: True)


def test_concurrent_provider_request_is_deferred_without_calling_network(db_engine):
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f"SELECT pg_advisory_lock({LOCK})"))
        try:
            with pytest.raises(ProviderCooldown) as paused:
                ProviderGate(db_engine, settings()).call(lambda: pytest.fail("network invoked"))
            assert paused.value.reason == "busy"
        finally:
            connection.execute(text(f"SELECT pg_advisory_unlock({LOCK})"))


@pytest.mark.parametrize(
    "code,error",
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (404, ProviderModelUnavailable),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
    ],
)
def test_provider_error_classes_are_separate(code, error):
    class Failure(Exception):
        status_code = code

    def fail(**kwargs):
        raise Failure("synthetic-private-detail")

    provider = object.__new__(GeminiProvider)
    provider.request_gate = None
    provider.client = SimpleNamespace(interactions=SimpleNamespace(create=fail))
    with pytest.raises(error) as caught:
        provider._create()
    assert "synthetic-private-detail" not in str(caught.value)


def test_request_boundary_uses_gate_before_network(db_engine):
    gate = ProviderGate(db_engine, settings(), lambda: NOW)
    gate.record("quota", NOW + timedelta(minutes=2))
    provider = object.__new__(GeminiProvider)
    provider.request_gate = gate
    provider.client = SimpleNamespace(
        interactions=SimpleNamespace(
            create=lambda **kwargs: pytest.fail("network called while paused")
        )
    )
    with pytest.raises(ProviderCooldown):
        provider._create(input="synthetic text")


def test_waiting_for_shared_cooldown_does_not_exhaust_job_attempts(db):
    from uuid import uuid4

    from garmin_ai.jobs import enqueue, finish
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    identity = enqueue(db, "telegram_update", {}, "synthetic-provider-paused", now)
    job = db.get(Job, identity)
    job.status, job.attempts = "running", 8
    job.lease_token = uuid4()
    job.lease_until = now + timedelta(minutes=1)
    db.flush()
    finish(db, job.id, job.lease_token, error_type="ProviderCooldown")
    assert job.status == "pending" and job.attempts == 7


def test_caught_quota_failure_still_queues_one_durable_notice(db, db_engine):
    from pydantic import SecretStr
    from sqlalchemy import select

    from garmin_ai.models import Job
    from garmin_ai.provider_gate import enqueue_quota_notice

    config = settings()
    config.telegram_user_id = 42
    config.telegram_bot_token = SecretStr("synthetic")
    gate = ProviderGate(db_engine, config, lambda: NOW)

    def rejected():
        raise ProviderRateLimited("synthetic")

    try:
        gate.call(rejected)
    except ProviderUnavailable:
        pass  # An offline form deliberately handles this without failing its job.
    enqueue_quota_notice(db, NOW)
    db.flush()
    jobs = db.scalars(select(Job).where(Job.kind == "telegram_provider_notice")).all()
    assert len(jobs) == 1 and jobs[0].status == "pending"
    assert jobs[0].payload == {"outbox_key": "quota:2026-09-10-00"}


def test_transport_error_has_the_same_retry_delay_as_its_gate(db, db_engine):
    gate = ProviderGate(db_engine, settings(), lambda: NOW)

    def rejected():
        raise ProviderUnavailable("synthetic")

    with pytest.raises(ProviderUnavailable) as error:
        gate.call(rejected)
    db.expire_all()
    deadline = datetime.fromisoformat(db.get(AppState, KEY).value["blocked_until"])
    assert (deadline - NOW).total_seconds() == error.value.retry_seconds == 60


def test_queued_quota_notice_is_delivered_without_a_model(db, db_engine, tmp_path, monkeypatch):
    import asyncio

    from sqlalchemy import select

    from garmin_ai import runtime
    from garmin_ai.models import Job
    from garmin_ai.provider_gate import QUOTA_NOTICE, enqueue_quota_notice

    enqueue_quota_notice(db, datetime.now(UTC))
    db.commit()
    messages = []

    class Bot:
        def __init__(self, *args):
            pass

        async def initialize(self):
            pass

        async def get_webhook_info(self):
            return SimpleNamespace(url="https://synthetic.invalid/hook")

        async def set_webhook(self, **kwargs):
            pass

        async def send_message(self, **kwargs):
            messages.append(kwargs["text"])
            return SimpleNamespace(message_id=123)

        async def shutdown(self):
            pass

    config = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="synthetic",
        telegram_webhook_secret="synthetic-webhook-secret",
        telegram_user_id=42,
        llm_enabled=False,
    )
    monkeypatch.setattr(runtime, "Bot", Bot)
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)
    monkeypatch.setattr(runtime.GarminReader, "restore", lambda _: runtime.GarminReader(None))
    monkeypatch.setattr(runtime, "run_garmin_job", lambda *args: None)

    async def run():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(), "add_signal_handler", lambda s, cb: callbacks.append(cb)
        )
        task = asyncio.create_task(runtime.run(config))
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                if QUOTA_NOTICE in messages:
                    break
            assert messages.count(QUOTA_NOTICE) == 1
        finally:
            callbacks[0]()
            await asyncio.wait_for(task, 3)

    asyncio.run(run())
    db.expire_all()
    assert db.scalar(select(Job).where(Job.kind == "telegram_provider_notice")).status == "done"


@pytest.mark.parametrize(
    "deadline", ["invalid", "2026-09-10T00:10:00", [], {"synthetic": True}, 123]
)
def test_malformed_deadline_allows_one_serialized_recovery_probe(db, db_engine, deadline):
    gate = ProviderGate(db_engine, settings(), lambda: NOW)
    gate.record("quota", NOW + timedelta(minutes=2))
    row = db.get(AppState, KEY, populate_existing=True)
    row.value = {**row.value, "blocked_until": deadline}
    db.commit()
    assert gate.call(lambda: "recovered") == "recovered"
    db.expire_all()
    assert db.get(AppState, KEY).value["reason"] == "ready"


def test_failed_recovery_probe_replaces_invalid_deadline_with_finite_cooldown(db, db_engine):
    gate = ProviderGate(db_engine, settings(), lambda: NOW)
    gate.record("quota", NOW + timedelta(minutes=2))
    row = db.get(AppState, KEY, populate_existing=True)
    row.value = {**row.value, "blocked_until": "invalid"}
    db.commit()
    calls = []

    def failure():
        calls.append(True)
        raise ProviderUnavailable("synthetic")

    with pytest.raises(ProviderUnavailable):
        gate.call(failure)
    with pytest.raises(ProviderCooldown):
        gate.call(failure)
    assert calls == [True]
    db.expire_all()
    assert db.get(AppState, KEY).value["blocked_until"] == (NOW + timedelta(seconds=60)).isoformat()


@pytest.mark.parametrize("value", [None, [], "synthetic", 42])
def test_non_object_gate_can_recover(db, db_engine, value):
    db.add(AppState(key=KEY, value=value))
    db.commit()
    gate = ProviderGate(db_engine, settings(), lambda: NOW)
    assert gate.call(lambda: True)
    db.expire_all()
    assert db.get(AppState, KEY).value["reason"] == "ready"
