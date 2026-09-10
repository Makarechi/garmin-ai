from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from garmin_ai.config import Settings
from garmin_ai.llm import GeminiProvider, ProviderUnavailable


def settings(categories=("health", "diary"), **overrides):
    values = dict(
        _env_file=None,
        llm_enabled=True,
        gemini_api_key="synthetic",
        gemini_model="synthetic",
        llm_consent={
            "provider": "gemini",
            "model": "synthetic",
            "categories": list(categories),
            "granted_at": datetime.now(UTC) - timedelta(minutes=1),
            "policy_revision": 1,
        },
    )
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize("change", ["missing", "model", "future", "disabled"])
def test_invalid_consent_blocks_client_creation(monkeypatch, change):
    config = settings()
    if change == "missing":
        config.llm_consent = None
    elif change == "model":
        config.gemini_model = "different"
    elif change == "future":
        config.llm_consent.granted_at = datetime.now(UTC) + timedelta(days=1)
    else:
        config.llm_enabled = False
    touched = []
    monkeypatch.setattr("garmin_ai.llm.genai.Client", lambda **kwargs: touched.append(True))
    with pytest.raises(ProviderUnavailable):
        GeminiProvider(config)
    assert not touched


@pytest.mark.parametrize("categories", [("audio",), ("health",), ("diary",)])
def test_structured_context_requires_both_categories(monkeypatch, categories):
    monkeypatch.setattr("garmin_ai.llm.genai.Client", lambda **kwargs: None)
    provider = GeminiProvider(settings(categories))
    touched = []
    monkeypatch.setattr(provider, "_create", lambda **kwargs: touched.append(True))
    with pytest.raises(ProviderUnavailable, match="consent"):
        provider.structured("synthetic", "synthetic", BaseModel)
    assert not touched


def test_audio_is_separate_and_revocation_applies_to_existing_client(monkeypatch):
    monkeypatch.setattr("garmin_ai.llm.genai.Client", lambda **kwargs: None)
    config = settings()
    provider = GeminiProvider(config)
    transmitted = []
    monkeypatch.setattr(
        provider,
        "_create",
        lambda **kwargs: (
            transmitted.append(kwargs) or SimpleNamespace(output_text='{"text":"synthetic"}')
        ),
    )
    with pytest.raises(ProviderUnavailable, match="consent"):
        provider.transcribe(b"synthetic voice", "audio/ogg")
    assert not transmitted
    config.llm_consent.categories.add("audio")
    assert provider.transcribe(b"synthetic voice", "audio/ogg") == "synthetic"
    config.llm_consent = None
    with pytest.raises(ProviderUnavailable, match="consent"):
        provider.transcribe(b"synthetic voice", "audio/ogg")
    assert len(transmitted) == 1


def test_approved_model_can_answer_but_configuration_change_cannot_reuse_consent(monkeypatch):
    monkeypatch.setattr("garmin_ai.llm.genai.Client", lambda **kwargs: None)
    config = settings()
    provider = GeminiProvider(config)
    sent = []

    class Result(BaseModel):
        text: str

    monkeypatch.setattr(
        provider,
        "_create",
        lambda **kwargs: sent.append(kwargs) or SimpleNamespace(output_text='{"text":"synthetic"}'),
    )
    assert provider.structured("instruction", "context", Result).text == "synthetic"
    assert "llm_consent" not in str(sent)
    config.gemini_model = "different"
    with pytest.raises(ProviderUnavailable, match="consent"):
        provider.structured("instruction", "context", Result)
    assert len(sent) == 1
