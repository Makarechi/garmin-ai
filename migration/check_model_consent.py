"""Compare model authorization and saved onboarding choices with pinned Python."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from capture_reference import isolated_engine
from pydantic import SecretStr
from sqlalchemy.orm import Session

from garmin_ai.config import IntegrationInstance, ProviderConsent, Settings
from garmin_ai.extension_tck import ModelProbe
from garmin_ai.integrations import ConsentGuardedModel
from garmin_ai.llm import GeminiProvider, ProviderCapabilityUnsupported, ProviderConsentRequired
from garmin_ai.models import AppState
from garmin_ai.provider_gate import require_onboarding_categories

AT = datetime(2026, 10, 25, 9, tzinfo=UTC)
KEY = "synthetic-model-auth"
CASES = {
    "primary": {},
    "fallback_order_and_duplicates": {"fallbacks": ["other", "backup", "main", "backup", "extra"]},
    "fallback_disabled": {"fallback_enabled": False},
    "no_fallback_consent": {"consent_fallbacks": []},
    "diary_only_blocks_health": {"scope": ["diary"], "categories": ["diary", "health"]},
    "missing_consent": {"missing": True},
    "disabled": {"enabled": False},
    "future_grant": {"future": True},
    "foreign_destination": {"consent_instance": "model:gemini:other"},
    "foreign_provider": {"consent_provider": "openai"},
    "foreign_model": {"consent_model": "different"},
    "rotated_key": {"key_changed": True},
    "changed_model": {"prepared_model": "old"},
    "voice_allowed": {"operation": "voice"},
    "voice_denied": {"operation": "voice", "scope": ["diary"]},
    "text_audio_invalid": {"categories": ["audio"]},
    "empty_text_invalid": {"categories": []},
    "configuration_checks_empty_scope": {"operation": "configure", "scope": ["audio"]},
    "openai_text": {"provider": "openai"},
    "anthropic_text": {"provider": "anthropic"},
    "openai_voice_absent": {"provider": "openai", "operation": "voice"},
    "anthropic_structured_absent": {"provider": "anthropic", "structured": False},
    "plugin_credential_rotated": {"provider": "openai", "key_changed": True},
    "plugin_configuration_changed": {"provider": "anthropic", "prepared_model": "old"},
}


class TestClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT.astimezone(tz) if tz else AT.replace(tzinfo=None)


def configure(case):
    provider = case.get("provider", "gemini")
    instance = f"model:{provider}:primary"
    consent = (
        None
        if case.get("missing")
        else ProviderConsent(
            provider=case.get("consent_provider", provider),
            provider_instance_id=case.get("consent_instance", instance),
            model=case.get("consent_model", "main"),
            fallback_models=case.get("consent_fallbacks", ["backup", "extra"]),
            categories=set(case.get("scope", ["diary", "health", "audio"])),
            granted_at=AT + timedelta(seconds=1 if case.get("future") else -1),
            policy_revision=1,
        )
    )
    return Settings(
        _env_file=None,
        llm_enabled=case.get("enabled", True),
        llm_consent=consent,
        gemini_model="main",
        gemini_api_key=SecretStr(KEY + "-rotated" if case.get("key_changed") else KEY),
        gemini_fallback_enabled=case.get("fallback_enabled", True),
        gemini_fallback_models=case.get("fallbacks", ["backup"]),
        integrations=[
            IntegrationInstance(
                id=instance,
                kind="model",
                provider=provider,
                config={"model": "main"},
                secret_refs={"api_key": "GA_PLUGIN_SYNTHETIC_POLICY_KEY"},
            )
        ],
    )


def answer(case):
    settings = configure(case)
    instance = settings.integrations[0]
    categories = set(case.get("categories", ["diary"]))
    kind = case.get("operation", "structured")
    if instance.provider == "gemini":
        adapter = object.__new__(GeminiProvider)
        adapter.settings = settings
        adapter.instance_id = instance.id
        adapter.model = case.get("prepared_model", "main")
        adapter._api_key_fingerprint = hashlib.sha256(KEY.encode()).digest()
        adapter.generation_config = {}
        adapter._create = lambda **_kwargs: SimpleNamespace(
            status="completed",
            output_text='{"text":"synthetic"}' if kind == "voice" else '{"urgent":false}',
        )
    else:
        provider = SimpleNamespace(
            structured=lambda _instruction, _prompt, schema: schema(urgent=False)
        )
        capabilities = (
            frozenset({"structured_output"}) if case.get("structured", True) else frozenset()
        )
        original = instance.model_copy(
            update={"config": {"model": case.get("prepared_model", "main")}}
        )
        with patch.dict("os.environ", {"GA_PLUGIN_SYNTHETIC_POLICY_KEY": KEY}):
            adapter = ConsentGuardedModel(provider, original, settings, capabilities)
        adapter._request = lambda request, *_args: request()
    active_key = KEY + "-rotated" if case.get("key_changed") else KEY
    with patch.dict("os.environ", {"GA_PLUGIN_SYNTHETIC_POLICY_KEY": active_key}):
        try:
            if kind == "configure":
                adapter._authorize(set())
            elif kind == "voice":
                categories = {"audio"}
                adapter.transcribe(b"synthetic", "audio/ogg")
            else:
                adapter.structured_for_categories("synthetic", "synthetic", ModelProbe, categories)
            if instance.provider == "gemini":
                return {
                    "models": adapter._model_chain(set() if kind == "configure" else categories)
                }
            return {"models": [instance.config["model"]]}
        except ProviderCapabilityUnsupported:
            return {"error": "UnsupportedCapability"}
        except ProviderConsentRequired:
            return {"error": "ConsentRequired"}
        except ValueError:
            return {"error": "InvalidCategories"}


def onboarding_cases(engine):
    cases = [
        (False, None, ["diary", "audio"]),
        (True, {}, []),
        (True, {}, ["health"]),
        (True, {"model_categories": ["diary", "other"]}, ["diary"]),
        (True, {"model_categories": ["diary"]}, ["diary", "health"]),
        (True, {"model_categories": ["audio"]}, ["audio"]),
        (True, {"model_categories": "diary"}, ["diary"]),
        (True, {"model_categories": ["diary", {}]}, ["diary"]),
    ]
    results = []
    key = "preferences:onboarding"
    for present, value, categories in cases:
        with Session(engine) as session, session.begin():
            previous = session.get(AppState, key)
            if previous is not None:
                session.delete(previous)
                session.flush()
            if present:
                session.add(AppState(key=key, value=value))
            try:
                require_onboarding_categories(session, categories)
            except (ProviderConsentRequired, TypeError):
                allowed = False
            else:
                allowed = True
            results.append([present, value, categories, allowed])
    return results


def record(path):
    engine = isolated_engine()
    try:
        with (
            patch("garmin_ai.llm.datetime", TestClock),
            patch("garmin_ai.integrations.datetime", TestClock),
        ):
            fixtures = [{"name": name, **case, **answer(case)} for name, case in CASES.items()]
        output = {"model": fixtures, "onboarding": onboarding_cases(engine)}
        path.write_text(json.dumps(output), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    record(parser.parse_args().output)
