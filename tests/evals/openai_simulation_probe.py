"""Explicit synthetic HTTP probe against a local Postman simulation only."""

import argparse
import os
from datetime import UTC, datetime
from urllib.parse import urlparse

from garmin_ai import openai_adapter
from garmin_ai.agent import SafetyScreen
from garmin_ai.config import IntegrationInstance, ProviderConsent, Settings
from garmin_ai.integrations import create_model_provider
from garmin_ai.llm import ProviderRateLimited, ProviderUnavailable


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--expect", choices=("success", "rate-limit", "timeout"), default="success")
    args = parser.parse_args()
    address = urlparse(args.url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Only an explicit local HTTP simulation URL is allowed")
    os.environ["GA_PLUGIN_OPENAI_SYNTHETIC"] = "synthetic-key"
    openai_adapter._RESPONSES_URL = args.url
    instance = IntegrationInstance(
        id="model:openai:synthetic",
        kind="model",
        provider="openai",
        config={"model": "synthetic-model", "structured_output": True, "timeout_seconds": 1},
        secret_refs={"api_key": "GA_PLUGIN_OPENAI_SYNTHETIC"},
    )
    settings = Settings(
        _env_file=None,
        llm_enabled=True,
        integrations=[instance],
        llm_consent=ProviderConsent(
            provider="openai",
            provider_instance_id=instance.id,
            model="synthetic-model",
            categories={"health", "diary"},
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            policy_revision=1,
        ),
    )
    provider = create_model_provider(settings)
    try:
        try:
            result = provider.structured("Synthetic instruction", "Synthetic input", SafetyScreen)
        except ProviderRateLimited:
            if args.expect != "rate-limit":
                raise
        except ProviderUnavailable as exc:
            if args.expect != "timeout" or type(exc) is not ProviderUnavailable:
                raise
        else:
            if args.expect != "success" or result != SafetyScreen(
                urgent=False, response_language="en"
            ):
                raise AssertionError("Unexpected simulation outcome")
        print(f"Synthetic simulation outcome: {args.expect}")
    finally:
        provider.close()


if __name__ == "__main__":
    main()
