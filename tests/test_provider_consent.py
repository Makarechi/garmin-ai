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
    touched = []
    monkeypatch.setattr("garmin_ai.llm.genai.Client", lambda **kwargs: touched.append(True))
    with pytest.raises(ProviderUnavailable, match="consent"):
        GeminiProvider(settings(categories))
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


def test_revoked_consent_does_not_leave_text_update_retrying(db, db_engine, monkeypatch):
    from garmin_ai.models import TelegramUpdate
    from garmin_ai.telegram import process_message, save_update

    monkeypatch.setattr("garmin_ai.llm.genai.Client", lambda **kwargs: None)
    config = settings(telegram_user_id=42)
    provider = GeminiProvider(config)
    config.llm_consent = None
    save_update(
        db,
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "synthetic",
            },
        },
        42,
    )
    db.commit()
    response = process_message(db_engine, provider, config, 1)
    assert "недоступна" in response
    assert db.get(TelegramUpdate, 1, populate_existing=True).status == "processed"
    assert process_message(db_engine, provider, config, 1) == response


@pytest.mark.parametrize("status", [429, 503])
def test_actual_sdk_does_not_retry_before_application_error_handling(monkeypatch, status):
    import httpx
    from google.genai._gaos.utils import retries

    from garmin_ai import llm
    from garmin_ai.llm import ProviderRateLimited

    requests = []
    sleeps = []

    def response(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Retry-After": "600"},
            json={
                "error": {
                    "code": status,
                    "message": "synthetic temporary error",
                    "status": "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE",
                }
            },
        )

    def sleep(seconds):
        sleeps.append(seconds)
        raise RuntimeError("Unexpected SDK backoff")

    transport = httpx.Client(transport=httpx.MockTransport(response))
    client_factory = llm.genai.Client

    def client(**kwargs):
        kwargs["http_options"]["httpx_client"] = transport
        return client_factory(**kwargs)

    monkeypatch.setattr(llm.genai, "Client", client)
    monkeypatch.setattr(retries.time, "sleep", sleep)

    class Parsed(BaseModel):
        value: bool

    provider = GeminiProvider(settings())
    try:
        with pytest.raises(ProviderRateLimited if status == 429 else ProviderUnavailable):
            provider.structured("Synthetic instruction", "Synthetic input", Parsed)
        assert len(requests) == 1 and sleeps == []
    finally:
        provider.close()
        transport.close()
