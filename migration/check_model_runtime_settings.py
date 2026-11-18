"""Exercise the pinned provider setup with synthetic credentials and no network requests."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from pydantic import SecretStr, ValidationError

from garmin_ai.config import Settings
from garmin_ai.integrations import configured_model_instance, create_model_provider
from garmin_ai.llm import ProviderConsentRequired


@patch.dict("os.environ", {}, clear=True)
def run():
    granted = datetime.now(UTC) - timedelta(hours=1)
    legacy = Settings(
        _env_file=None,
        llm_enabled=True,
        gemini_api_key="synthetic-provider-key-one",
        gemini_model="synthetic-primary",
        gemini_fallback_models=["synthetic-unapproved", "synthetic-fallback"],
        llm_consent={
            "provider": "gemini",
            "model": "synthetic-primary",
            "fallback_models": ["synthetic-fallback"],
            "categories": ["diary"],
            "granted_at": granted,
            "policy_revision": 1,
        },
    )
    with (
        patch("garmin_ai.llm.genai.Client", return_value=MagicMock()),
        patch("garmin_ai.llm.GeminiProvider._prepare_interactions"),
    ):
        model = create_model_provider(legacy)
        assert model.instance_id == "model:gemini:primary"
        assert model.request_deadline_seconds is None
        assert model._model_chain({"diary"}) == ["synthetic-primary", "synthetic-fallback"]
        legacy.gemini_api_key = SecretStr("synthetic-provider-key-two")
        try:
            model._model_chain({"diary"})
        except ProviderConsentRequired:
            pass
        else:
            raise AssertionError("The replaced credential still authorized the original client")
        model.close()
    assert configured_model_instance(legacy.model_copy(update={"integrations": []})) is None
    channel = {"id": "channel:telegram:synthetic", "kind": "channel", "provider": "telegram"}
    assert (
        configured_model_instance(
            legacy.model_copy(
                update={
                    "integrations": Settings(_env_file=None, integrations=[channel]).integrations
                }
            )
        )
        is None
    )

    for provider in ("openai", "anthropic"):
        instance = f"model:{provider}:synthetic"
        reference = "GA_PLUGIN_SELECTED_SYNTHETIC"
        integration = {
            "id": instance,
            "kind": "model",
            "provider": provider,
            "request_timeout_seconds": 31.5,
            "config": {
                "model": "synthetic-model",
                "structured_output": True,
                "timeout_seconds": 12.5,
                "max_output_tokens": 512,
            },
            "secret_refs": {"api_key": reference},
        }
        with patch.dict("os.environ", {reference: "synthetic-provider-key"}, clear=True):
            settings = Settings(
                _env_file=None,
                llm_enabled=True,
                integrations=[integration],
                llm_consent={
                    "provider": provider,
                    "provider_instance_id": instance,
                    "model": "synthetic-model",
                    "categories": ["diary", "audio"],
                    "granted_at": granted,
                    "policy_revision": 1,
                },
            )
            model = create_model_provider(settings)
            assert model.instance_id == instance
            assert model.capabilities == {"structured_output"}
            assert model.instance.request_timeout_seconds == 31.5
            assert model.provider.config.timeout_seconds == 12.5
            assert model.provider.config.max_output_tokens == 512
            model.close()
        try:
            Settings(
                _env_file=None,
                integrations=[{**integration, "secret_refs": {"api_key": "GA_DATABASE_URL"}}],
            )
        except ValidationError:
            pass
        else:
            raise AssertionError("A database variable was accepted as a model credential reference")
    print("Verified original Gemini, OpenAI and Anthropic settings without provider requests")


if __name__ == "__main__":
    run()
