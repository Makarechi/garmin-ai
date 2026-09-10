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
