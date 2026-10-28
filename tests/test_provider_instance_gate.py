"""Durable model-instance isolation using synthetic providers and database state."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from garmin_ai.config import IntegrationInstance, ProviderConsent, Settings
from garmin_ai.integrations import ConsentGuardedModel
from garmin_ai.llm import ProviderConsentRequired, ProviderCooldown, ProviderRateLimited
from garmin_ai.models import AppState
from garmin_ai.provider_gate import paused


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
