import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.events import EventInput
from garmin_ai.integrations import (
    IntegrationFactory,
    IntegrationRegistry,
    IntegrationUnavailable,
    channel_instance_id,
    configured_instance,
    configured_instances,
    default_registry,
    integration_statuses,
    require_capability,
)


def test_fake_model_provider_uses_registry_without_changing_domain_events():
    registry = IntegrationRegistry()
    created = []
    registry.register(
        IntegrationFactory(
            kind="model",
            provider="fake",
            factory=lambda _settings, instance_id: created.append(instance_id) or {"fake": True},
            capabilities=frozenset({"structured_output"}),
        )
    )
    instance = IntegrationInstance(id="model:fake:test", kind="model", provider="fake")

    assert registry.create(instance, Settings()) == {"fake": True}
    assert created == ["model:fake:test"]
    assert "provider" not in EventInput.model_fields


def test_missing_optional_package_has_explicit_status(monkeypatch):
    monkeypatch.setattr("garmin_ai.integrations.module_available", lambda _name: False)
    instance = IntegrationInstance(id="source:garmin:test", kind="source", provider="garmin")

    status = default_registry().status(instance)

    assert not status.available
    assert status.capabilities == frozenset()
    assert "missing optional package" in status.reason
    with pytest.raises(IntegrationUnavailable, match="missing optional package"):
        default_registry().create(instance, Settings())


def test_unsupported_capability_is_reported_instead_of_promised():
    registry = IntegrationRegistry()
    registry.register(
        IntegrationFactory(
            kind="source",
            provider="fake",
            factory=lambda *_args: object(),
            capabilities=frozenset({"heart_rate"}),
        )
    )
    status = registry.status(
        IntegrationInstance(id="source:fake:test", kind="source", provider="fake")
    )

    require_capability(status, "heart_rate")
    with pytest.raises(IntegrationUnavailable, match="unsupported capability: sleep"):
        require_capability(status, "sleep")


def test_telegram_registry_advertises_only_adapter_delivery_capabilities():
    descriptor = default_registry().descriptor("channel", "telegram")

    assert descriptor.capabilities == frozenset({"text", "actions", "initiatives"})


def test_explicit_configuration_does_not_enable_omitted_or_disabled_integrations():
    settings = Settings(
        integrations=[
            {
                "id": "source:garmin:disabled",
                "kind": "source",
                "provider": "garmin",
                "enabled": False,
            },
            {
                "id": "model:fake:primary",
                "kind": "model",
                "provider": "fake",
            },
        ]
    )

    assert configured_instance(settings, "source", "garmin") is None
    assert configured_instance(settings, "channel", "telegram") is None
    assert configured_instance(settings, "model", "fake").id == "model:fake:primary"
    disabled = integration_statuses(settings)[0]
    assert not disabled.available
    assert disabled.reason == "integration is disabled"


def test_explicit_integrations_require_runtime_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr("garmin_ai.integrations.module_available", lambda _name: True)
    settings = Settings(
        token_dir=tmp_path / "tokens",
        integrations=[
            IntegrationInstance(id="source:garmin:test", kind="source", provider="garmin"),
            IntegrationInstance(id="channel:telegram:test", kind="channel", provider="telegram"),
            IntegrationInstance(id="model:gemini:test", kind="model", provider="gemini"),
        ],
    )

    statuses = {row.instance_id: row for row in integration_statuses(settings)}

    assert not statuses["source:garmin:test"].available
    assert "tokens" in statuses["source:garmin:test"].reason
    assert not statuses["channel:telegram:test"].available
    assert "token and owner" in statuses["channel:telegram:test"].reason
    assert not statuses["model:gemini:test"].available
    assert "credentials" in statuses["model:gemini:test"].reason


def test_channel_instance_id_uses_configured_stable_suffix():
    instance = IntegrationInstance(
        id="channel:telegram:private", kind="channel", provider="telegram"
    )

    assert channel_instance_id(instance) == "private"


def test_explicit_empty_integration_allowlist_disables_legacy_discovery():
    settings = Settings(
        integrations=[],
        telegram_bot_token="synthetic-secret",
        telegram_user_id=42,
        gemini_api_key="synthetic-model-secret",
        gemini_model="synthetic-model",
        llm_enabled=True,
    )

    assert configured_instances(settings) == []
    assert configured_instance(settings, "channel", "telegram") is None
    assert configured_instance(settings, "model", "gemini") is None


def test_legacy_settings_map_to_stable_instance_ids_without_exposing_secrets(tmp_path):
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "garmin_tokens.json").write_text("synthetic")
    settings = Settings(
        token_dir=token_dir,
        data_dir=tmp_path / "data",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        telegram_bot_token="synthetic-secret",
        telegram_user_id=42,
        gemini_api_key="synthetic-model-secret",
        gemini_model="synthetic-model",
        llm_enabled=True,
    )

    instances = configured_instances(settings)
    assert [item.id for item in instances] == [
        "source:garmin:primary",
        "channel:telegram:primary",
        "model:gemini:primary",
    ]
    assert "synthetic-secret" not in str(instances)
    assert "synthetic-model-secret" not in str(integration_statuses(settings))


def test_empty_legacy_token_directory_does_not_advertise_garmin(tmp_path):
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()

    instances = configured_instances(
        Settings(
            token_dir=token_dir,
            data_dir=tmp_path / "data",
            lock_dir=tmp_path / "locks",
            backup_dir=tmp_path / "backups",
        )
    )

    assert (
        configured_instance(
            Settings(
                token_dir=token_dir,
                data_dir=tmp_path / "data",
                lock_dir=tmp_path / "locks",
                backup_dir=tmp_path / "backups",
            ),
            "source",
            "garmin",
        )
        is None
    )
    assert all(item.provider != "garmin" for item in instances)


def test_core_cli_and_model_contract_import_without_optional_sdks():
    script = textwrap.dedent(
        """
        import builtins
        import importlib
        import sys

        blocked = ("garminconnect", "google", "telegram")
        original = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name in blocked or name.startswith(tuple(item + "." for item in blocked)):
                raise ImportError(f"blocked optional import: {name}")
            return original(name, *args, **kwargs)

        builtins.__import__ = guarded
        for name in (
            "garmin_ai.cli",
            "garmin_ai.healthcheck",
            "garmin_ai.llm",
            "garmin_ai.observability",
            "garmin_ai.runtime",
        ):
            importlib.import_module(name)
        assert importlib.import_module("garmin_ai.runtime").DiaryDeferred.__name__ == "DiaryDeferred"
        assert "garminconnect" not in sys.modules
        assert "telegram" not in sys.modules
        assert "google.genai" not in sys.modules
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_webhook_rejects_an_explicitly_disabled_telegram_integration(db, db_engine):
    from fastapi.testclient import TestClient

    from garmin_ai.api import create_app
    from garmin_ai.models import TelegramUpdate

    secret = "synthetic-webhook-secret-32-characters"
    settings = Settings(
        integrations=[],
        telegram_user_id=42,
        telegram_webhook_secret=secret,
    )
    with TestClient(create_app(settings, db_engine)) as client:
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            json={
                "update_id": 5001,
                "message": {
                    "message_id": 5001,
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": "synthetic disabled ingress",
                },
            },
        )

    assert response.status_code == 503
    assert db.get(TelegramUpdate, 5001) is None


def test_webhook_uses_the_configured_telegram_instance_namespace(db, db_engine):
    from fastapi.testclient import TestClient

    from garmin_ai.api import create_app
    from garmin_ai.models import InboundMessage

    secret = "synthetic-webhook-secret-32-characters"
    settings = Settings(
        integrations=[
            IntegrationInstance(
                id="channel:telegram:private",
                kind="channel",
                provider="telegram",
            )
        ],
        telegram_user_id=42,
        telegram_webhook_secret=secret,
    )
    with TestClient(create_app(settings, db_engine)) as client:
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            json={
                "update_id": 5002,
                "message": {
                    "message_id": 5002,
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": "synthetic configured ingress",
                },
            },
        )

    assert response.status_code == 200
    assert db.scalar(select(InboundMessage)).channel_instance_id == "private"


def test_webhook_honors_completed_onboarding_channel_selection(db, db_engine):
    from fastapi.testclient import TestClient

    from garmin_ai.api import create_app
    from garmin_ai.models import AppState

    db.add(
        AppState(
            key="preferences:onboarding",
            value={"channel": None, "model_categories": [], "source_instance_ids": []},
        )
    )
    db.commit()
    secret = "synthetic-onboarding-webhook-secret"
    settings = Settings(
        telegram_user_id=42,
        telegram_webhook_secret=secret,
    )

    with TestClient(create_app(settings, db_engine)) as client:
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            json={
                "update_id": 5003,
                "message": {
                    "message_id": 5003,
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": "synthetic onboarding-disabled ingress",
                },
            },
        )

    assert response.status_code == 503


def test_model_consent_is_scoped_to_stable_instance_id(monkeypatch):
    from datetime import UTC, datetime

    from garmin_ai.llm import GeminiProvider, ProviderUnavailable

    monkeypatch.setattr(
        "garmin_ai.llm.genai",
        SimpleNamespace(Client=lambda **_kwargs: object()),
    )
    settings = Settings(
        llm_enabled=True,
        gemini_api_key="synthetic",
        gemini_model="synthetic-model",
        llm_consent={
            "provider": "gemini",
            "provider_instance_id": "model:gemini:approved",
            "model": "synthetic-model",
            "categories": ["health", "diary"],
            "granted_at": datetime.now(UTC),
            "policy_revision": 1,
        },
    )

    GeminiProvider(settings, instance_id="model:gemini:approved")
    with pytest.raises(ProviderUnavailable, match="consent"):
        GeminiProvider(settings, instance_id="model:gemini:different")
