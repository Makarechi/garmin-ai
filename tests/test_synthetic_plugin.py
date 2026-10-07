"""Installed, separate-package extension contract on synthetic data only."""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from garmin_ai.agent import screen_reply_safety
from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, IntegrationInstance, ProviderConsent, Settings
from garmin_ai.extension_tck import check_model_adapter
from garmin_ai.integrations import (
    IntegrationFactory,
    IntegrationUnavailable,
    configured_model_instance,
    create_model_provider,
    default_registry,
    integration_statuses,
)
from garmin_ai.llm import ProviderCapabilityUnsupported, ProviderConsentRequired
from garmin_ai.models import AppState, Event
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)


@pytest.fixture(autouse=True)
def installed_fixture():
    pytest.importorskip("synthetic_model")


def settings_for(instance_id="model:synthetic:one", response="none"):
    return Settings(
        integrations=[
            IntegrationInstance(
                id=instance_id,
                kind="model",
                provider="synthetic",
                config={"model": "fixed-v1", "response": response},
            )
        ],
        llm_enabled=True,
        telegram_bot_token="",
        telegram_user_id=0,
        backup_key="",
        llm_consent=ProviderConsent(
            provider="synthetic",
            provider_instance_id=instance_id,
            model="fixed-v1",
            categories={"health", "diary"},
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            policy_revision=1,
        ),
    )


def test_explicit_discovery_and_agent_handler(monkeypatch):
    settings = settings_for()
    provider = create_model_provider(settings)
    assert provider.instance_id == "model:synthetic:one"
    assert screen_reply_safety(provider, "ordinary synthetic note").intent == "clarify"
    assert provider.provider.calls == 1
    assert check_model_adapter(provider) == {"structured_output": True}
    assert provider.provider.closed
    provider = create_model_provider(settings)
    with pytest.raises(ProviderCapabilityUnsupported, match="does not support transcription"):
        provider.transcribe(b"synthetic", "audio/ogg")
    provider.close()
    assert provider.provider.closed

    disabled = settings.model_copy(
        update={"integrations": [settings.integrations[0].model_copy(update={"enabled": False})]}
    )
    assert create_model_provider(disabled) is None
    assert integration_statuses(disabled)[0].reason == "integration is disabled"

    empty = settings.model_copy(update={"integrations": []})
    assert configured_model_instance(empty) is None
    assert create_model_provider(empty) is None

    monkeypatch.setattr("garmin_ai.integrations.entry_points", lambda **_kwargs: [])
    assert integration_statuses(settings)[0].reason == "integration provider is not registered"
    assert create_model_provider(empty) is None


def test_persisted_onboarding_revoke_stops_existing_plugin(db, db_engine):
    provider = create_model_provider(settings_for(), db_engine)
    screen_reply_safety(provider, "first synthetic note")
    calls = provider.provider.calls
    db.add(
        AppState(
            key="preferences:onboarding",
            value={"model_categories": []},
        )
    )
    db.commit()
    with pytest.raises(ProviderConsentRequired, match="Onboarding model choices"):
        screen_reply_safety(provider, "second synthetic note")
    assert provider.provider.calls == calls
    db.get(AppState, "preferences:onboarding").value = {"model_categories": ["health", "diary"]}
    db.commit()
    screen_reply_safety(provider, "third synthetic note")
    assert provider.provider.calls == calls + 1
    provider.close()


def test_instance_configuration_secret_scope_and_consent(monkeypatch):
    from synthetic_model import SyntheticProvider

    monkeypatch.setenv("GA_PLUGIN_FIRST", "synthetic-first")
    monkeypatch.setenv("GA_PLUGIN_SECOND", "synthetic-second")
    settings = settings_for()
    first = settings.integrations[0].model_copy(
        update={"secret_refs": {"token": "GA_PLUGIN_FIRST"}}
    )
    second = first.model_copy(
        update={
            "id": "model:synthetic:two",
            "config": {"model": "other-v1", "response": "clarify"},
            "secret_refs": {"token": "GA_PLUGIN_SECOND"},
        }
    )
    both = settings.model_copy(update={"integrations": [first, second]})
    registry = default_registry(both)
    one = registry.create(first, both)
    two = registry.create(second, both)
    assert one.instance_id != two.instance_id
    assert one.config.model == "fixed-v1" and two.config.model == "other-v1"
    assert one.received_secret_names == two.received_secret_names == {"token"}
    assert one.secret_fingerprints["token"] != two.secret_fingerprints["token"]
    one.calls += 1
    assert two.calls == 0
    assert "synthetic-first" not in repr(one)
    assert "synthetic-second" not in repr(two)
    one.close()
    assert one.closed and not two.closed

    bad = settings.model_copy(update={"integrations": [second]})
    before = len(SyntheticProvider.instances)
    with pytest.raises(Exception, match="consent"):
        create_model_provider(bad)
    assert len(SyntheticProvider.instances) == before
    assert not two.closed
    with pytest.raises(ValueError, match="one enabled model"):
        configured_model_instance(settings.model_copy(update={"integrations": [first, second]}))

    monkeypatch.delenv("GA_PLUGIN_FIRST")
    status = registry.status(first, settings)
    assert not status.available and status.reason == "missing secret reference: token"
    with pytest.raises(IntegrationUnavailable, match="missing secret reference"):
        registry.create(first, both)


def test_plugin_load_failure_does_not_disable_core(monkeypatch):
    class BrokenEntry:
        name = "model.synthetic"

        def load(self):
            raise RuntimeError("synthetic plugin failure")

    monkeypatch.setattr("garmin_ai.integrations.entry_points", lambda **_kwargs: [BrokenEntry()])
    settings = settings_for()
    status = integration_statuses(settings)[0]
    assert not status.available
    assert status.reason == "integration plugin failed to load"
    with pytest.raises(IntegrationUnavailable, match="failed to load"):
        create_model_provider(settings)
    assert create_model_provider(settings.model_copy(update={"integrations": []})) is None


def test_transcription_only_plugin_cannot_be_selected_as_text_model(monkeypatch):
    calls = []

    class AudioOnlyEntry:
        name = "model.synthetic"

        def load(self):
            return IntegrationFactory(
                kind="model",
                provider="synthetic",
                plugin_factory=lambda context: calls.append(context),
                capabilities=frozenset({"transcription"}),
            )

    monkeypatch.setattr("garmin_ai.integrations.entry_points", lambda **_kwargs: [AudioOnlyEntry()])
    settings = settings_for()
    status = integration_statuses(settings)[0]
    assert not status.available
    assert status.reason == "model plugin lacks structured_output capability"
    with pytest.raises(IntegrationUnavailable, match="lacks structured_output"):
        create_model_provider(settings)
    assert calls == []


def test_installed_plugin_http_repeated_operation_writes_one_fact(db, db_engine):
    draft = TrackerSetupDraft(
        key="stretch",
        name="Растяжка",
        locale="ru",
        topology="bounded_interval",
        fields=[
            TrackerFieldDraft(
                key="difficulty", label="Сложность", kind="scale", minimum=1, maximum=5
            )
        ],
    )
    preview = preview_tracker(db, draft)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="synthetic-plugin-test",
    )
    db.commit()
    api_key = "synthetic-plugin-test-" + "x" * 32
    settings = settings_for(response="create_entry").model_copy(
        update={"api_tokens": [ApiToken(key=api_key, scopes={"read:diary", "write:diary"})]}
    )
    client = TestClient(create_app(settings, db_engine))
    request = {
        "text": "2026-09-20 19:00 to 2026-09-20 19:15; difficulty 3",
        "operation_id": "synthetic-plugin-repeat",
    }
    headers = {"Authorization": "Bearer " + api_key}
    first = client.post("/natural-language/trackers", json=request, headers=headers).json()
    second = client.post("/natural-language/trackers", json=request, headers=headers).json()
    assert first == second and first.get("written"), first
    assert (
        db.scalar(select(func.count()).select_from(Event).where(Event.id == first["event_id"])) == 1
    )


def test_runtime_starts_and_closes_installed_plugin(db, db_engine, tmp_path, monkeypatch):
    from synthetic_model import SyntheticProvider

    from garmin_ai import runtime

    db.commit()
    settings = settings_for().model_copy(
        update={
            "data_dir": tmp_path / "data",
            "token_dir": tmp_path / "tokens",
            "lock_dir": tmp_path / "locks",
            "backup_dir": tmp_path / "backups",
        }
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _settings: db_engine)
    before = len(SyntheticProvider.instances)

    async def scenario():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "add_signal_handler",
            lambda _signal, cb: callbacks.append(cb),
        )
        task = asyncio.create_task(runtime.run(settings))
        for _ in range(100):
            if len(SyntheticProvider.instances) > before and callbacks:
                break
            await asyncio.sleep(0.01)
        assert len(SyntheticProvider.instances) == before + 1
        callbacks[0]()
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(scenario())
    assert SyntheticProvider.instances[-1].closed
