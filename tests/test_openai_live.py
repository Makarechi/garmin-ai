"""Explicitly enabled, synthetic-only OpenAI smoke check; never run by default."""

import hashlib
import json
import os
from datetime import UTC, datetime

import pytest

from garmin_ai.agent import SafetyScreen
from garmin_ai.config import IntegrationInstance, ProviderConsent, Settings
from garmin_ai.integrations import create_model_provider
from garmin_ai.openai_schema import OpenAISchemaCompiler
from garmin_ai.prompts import load_prompt

pytestmark = pytest.mark.skipif(
    os.environ.get("GA_LIVE_OPENAI_TESTS") != "1", reason="Explicit live OpenAI opt-in required"
)


def test_selected_openai_model_handles_synthetic_safety_screen(record_property):
    model = os.environ.get("GA_OPENAI_LIVE_MODEL")
    if not model or not os.environ.get("GA_PLUGIN_OPENAI_LIVE_KEY"):
        pytest.fail("Explicit model and secret reference are required for the live smoke check")
    instance = IntegrationInstance(
        id="model:openai:live-eval",
        kind="model",
        provider="openai",
        config={"model": model, "structured_output": True},
        secret_refs={"api_key": "GA_PLUGIN_OPENAI_LIVE_KEY"},
    )
    settings = Settings(
        _env_file=None,
        llm_enabled=True,
        integrations=[instance],
        llm_consent=ProviderConsent(
            provider="openai",
            provider_instance_id=instance.id,
            model=model,
            categories={"health", "diary"},
            granted_at=datetime.now(UTC),
            policy_revision=1,
        ),
    )
    instruction = load_prompt("safety_reply")
    schema = OpenAISchemaCompiler().compile(SafetyScreen)
    record_property("configured_model", model)
    record_property("prompt_sha256", hashlib.sha256(instruction.encode()).hexdigest())
    record_property(
        "schema_sha256", hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    )
    provider = create_model_provider(settings)
    try:
        result = provider.structured(
            instruction, "This is a fictional ordinary message.", SafetyScreen
        )
        assert not result.urgent
    finally:
        provider.close()
