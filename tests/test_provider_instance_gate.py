"""Durable model-instance isolation using synthetic providers and database state."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from garmin_ai.config import IntegrationInstance, ProviderConsent, Settings
from garmin_ai.integrations import ConsentGuardedModel
from garmin_ai.llm import (
    ProviderConsentRequired,
    ProviderCooldown,
    ProviderOutputInvalid,
    ProviderRateLimited,
)
from garmin_ai.models import AppState, Job
from garmin_ai.provider_gate import ACTIVE_JOB, KEY, ProviderGate, configuration_key, paused


class Result(BaseModel):
    answer: str


class SyntheticProvider:
    def __init__(self, *, limited=False):
        self.limited = limited
        self.calls = 0

    def structured(self, _instruction, _prompt, _schema):
        self.calls += 1
        if self.limited:
            raise ProviderRateLimited("synthetic rate limit")
        return Result(answer="ok")

    def close(self):
        pass


def guarded(engine, identity, provider, *, secret_refs=None, config=None):
    instance = IntegrationInstance(
        id=identity,
        kind="model",
        provider="synthetic",
        config=config or {"model": "test-v1"},
        secret_refs=secret_refs or {},
    )
    settings = Settings(
        _env_file=None,
        integrations=[instance],
        llm_enabled=True,
        telegram_bot_token="",
        telegram_user_id=0,
        llm_consent=ProviderConsent(
            provider="synthetic",
            provider_instance_id=identity,
            model="test-v1",
            categories={"health", "diary"},
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            policy_revision=1,
        ),
    )
    return ConsentGuardedModel(
        provider, instance, settings, frozenset({"structured_output"}), engine
    )


def test_rate_limit_survives_restart_without_blocking_another_instance(db, db_engine, monkeypatch):
    monkeypatch.setenv("GA_PLUGIN_SYNTHETIC_TEST_SECRET", "synthetic-secret-value")
    refs = {"token": "GA_PLUGIN_SYNTHETIC_TEST_SECRET"}
    first_provider = SyntheticProvider(limited=True)
    first = guarded(db_engine, "model:synthetic:first", first_provider, secret_refs=refs)
    with pytest.raises(ProviderRateLimited):
        first.structured("instruction", "synthetic private prompt", Result)
    assert first_provider.calls == 1
    restarted_provider = SyntheticProvider()
    restarted = guarded(
        db_engine,
        "model:synthetic:first",
        restarted_provider,
        secret_refs=refs,
        config={"model": "test-v1", "option": "changed during upgrade"},
    )
    with pytest.raises(ProviderCooldown):
        restarted.structured("instruction", "synthetic private prompt", Result)
    assert restarted_provider.calls == 0
    second_provider = SyntheticProvider()
    second = guarded(db_engine, "model:synthetic:second", second_provider)
    assert second.structured("instruction", "synthetic private prompt", Result).answer == "ok"
    assert second_provider.calls == 1
    assert paused(db, settings=first.settings)
    assert not paused(db, settings=second.settings)
    db.expire_all()
    values = str([row.value for row in db.query(AppState).all()])
    assert "synthetic private prompt" not in values
    assert "synthetic-secret-value" not in values

    monkeypatch.setenv("GA_PLUGIN_SYNTHETIC_TEST_SECRET", "synthetic-second-secret")
    replacement = guarded(db_engine, "model:synthetic:first", SyntheticProvider(), secret_refs=refs)
    assert replacement.structured("instruction", "synthetic", Result).answer == "ok"
    monkeypatch.setenv("GA_PLUGIN_SYNTHETIC_TEST_SECRET", "synthetic-secret-value")
    original = guarded(db_engine, "model:synthetic:first", SyntheticProvider(), secret_refs=refs)
    with pytest.raises(ProviderCooldown):
        original.structured("instruction", "synthetic", Result)


def test_revoked_onboarding_stops_an_existing_instance_before_the_next_request(db, db_engine):
    provider = SyntheticProvider()
    model = guarded(db_engine, "model:synthetic:first", provider)
    assert model.structured("instruction", "synthetic", Result).answer == "ok"
    db.add(AppState(key="preferences:onboarding", value={"model_categories": []}))
    db.commit()
    with pytest.raises(ProviderConsentRequired):
        model.structured("instruction", "synthetic", Result)
    assert provider.calls == 1


def test_busy_instance_lock_does_not_block_another_instance(db, db_engine):
    first_provider = SyntheticProvider()
    first = guarded(db_engine, "model:synthetic:first", first_provider)
    second_provider = SyntheticProvider()
    second = guarded(db_engine, "model:synthetic:second", second_provider)
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        assert connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": first.request_gate.lock_key}
        )
        try:
            with pytest.raises(ProviderCooldown):
                first.structured("instruction", "synthetic", Result)
            assert first_provider.calls == 0
            assert second.structured("instruction", "synthetic", Result).answer == "ok"
            assert second_provider.calls == 1
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": first.request_gate.lock_key}
            )


def gemini_gate(engine, identity, now):
    instance = IntegrationInstance(id=identity, kind="model", provider="gemini")
    settings = Settings(
        _env_file=None,
        integrations=[instance],
        gemini_model="synthetic-model",
        gemini_api_key="synthetic-key",
        telegram_bot_token="",
        telegram_user_id=0,
    )
    return ProviderGate(engine, settings, lambda: now, instance=instance), settings


def test_gemini_instances_have_separate_cooldowns_and_legacy_primary_key(db, db_engine):
    now = datetime(2026, 10, 22, 14, tzinfo=UTC)
    primary, _ = gemini_gate(db_engine, "model:gemini:primary", now)
    first, _ = gemini_gate(db_engine, "model:gemini:first", now)
    second, _ = gemini_gate(db_engine, "model:gemini:second", now)

    def limited():
        raise ProviderRateLimited("synthetic quota")

    with pytest.raises(ProviderRateLimited):
        primary.call(limited)
    assert first.call(lambda: "ok") == "ok"
    with pytest.raises(ProviderRateLimited):
        first.call(limited)
    assert second.call(lambda: "ok") == "ok"
    assert primary.state_key == KEY
    assert len({primary.state_key, first.state_key, second.state_key}) == 3


def test_named_gemini_keeps_an_unmarked_legacy_cooldown_on_upgrade(db, db_engine):
    now = datetime(2026, 10, 22, 14, tzinfo=UTC)
    gate, settings = gemini_gate(db_engine, "model:gemini:secondary", now)
    db.add(
        AppState(
            key=KEY,
            value={
                "configuration": configuration_key(settings),
                "reason": "quota",
                "blocked_until": (now + timedelta(minutes=10)).isoformat(),
            },
        )
    )
    db.commit()
    with pytest.raises(ProviderCooldown):
        gate.call(lambda: pytest.fail("Legacy cooldown must block the request"))
    assert paused(db, now, settings=settings)


def test_named_gemini_preserves_legacy_scheduler_pause_during_first_success(db, db_engine):
    now = datetime(2026, 10, 22, 14, tzinfo=UTC)
    gate, settings = gemini_gate(db_engine, "model:gemini:secondary", now)
    deadline = now + timedelta(minutes=10)
    db.add(
        AppState(
            key=KEY,
            value={
                "configuration": configuration_key(settings),
                "reason": "ready",
                "blocked_until": None,
                "scheduler_pause_until": deadline.isoformat(),
            },
        )
    )
    db.commit()
    assert gate.call(lambda: "ok") == "ok"
    assert paused(db, now, settings=settings)


def test_primary_success_preserves_unmarked_legacy_pause_for_named_instance(db, db_engine):
    now = datetime(2026, 10, 22, 14, tzinfo=UTC)
    primary, settings = gemini_gate(db_engine, "model:gemini:primary", now)
    _, named_settings = gemini_gate(db_engine, "model:gemini:secondary", now)
    db.add(
        AppState(
            key=KEY,
            value={
                "configuration": configuration_key(settings),
                "scheduler_pause_until": (now + timedelta(minutes=10)).isoformat(),
            },
        )
    )
    db.commit()
    assert primary.call(lambda: "ok") == "ok"
    db.expire_all()
    assert paused(db, now, settings=named_settings)


def test_named_gemini_does_not_repeat_ambiguous_legacy_job(db, db_engine):
    from garmin_ai.jobs import enqueue

    now = datetime(2026, 10, 22, 14, tzinfo=UTC)
    gate, _ = gemini_gate(db_engine, "model:gemini:secondary", now)
    job_id = enqueue(db, "telegram_update", {}, "synthetic-legacy-job", now)
    job = db.get(Job, job_id)
    job.payload = {
        "provider_resume": {
            "configuration": gate.configuration,
            "models": ["primary"],
            "request_fingerprint": sha256(repr((["primary"], [], {})).encode()).hexdigest(),
            "invalid_models": ["primary"],
        }
    }
    db.commit()
    token = ACTIVE_JOB.set(job_id)
    try:
        with pytest.raises(ProviderOutputInvalid):
            gate.call(
                lambda **kwargs: pytest.fail("Ambiguous legacy job must not be repeated"),
                models=["primary"],
            )
    finally:
        ACTIVE_JOB.reset(token)


def test_named_gemini_does_not_reuse_another_instances_job_progress(db, db_engine):
    from garmin_ai.jobs import enqueue

    now = datetime(2026, 10, 22, 14, tzinfo=UTC)
    first, _ = gemini_gate(db_engine, "model:gemini:first", now)
    second, _ = gemini_gate(db_engine, "model:gemini:second", now)
    assert first.configuration == second.configuration
    job_id = enqueue(db, "telegram_update", {}, "synthetic-instance-resume", now)
    db.commit()
    fingerprint = sha256(repr((["primary"], [], {})).encode()).hexdigest()
    assert first.record_resume(job_id, ["primary"], fingerprint, {"primary"})
    token = ACTIVE_JOB.set(job_id)
    try:
        assert second.call(lambda *, model: "executed", models=["primary"]) == "executed"
    finally:
        ACTIVE_JOB.reset(token)
