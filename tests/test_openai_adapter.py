"""Synthetic Responses API and provider-boundary checks; no external requests."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from garmin_ai.agent import SafetyScreen
from garmin_ai.config import IntegrationInstance, ProviderConsent, Settings
from garmin_ai.integrations import create_model_provider, integration_statuses
from garmin_ai.llm import (
    ProviderAuthError,
    ProviderCapabilityUnsupported,
    ProviderConsentRequired,
    ProviderOutputInvalid,
    ProviderOutputTruncated,
    ProviderRateLimited,
    ProviderRefused,
    ProviderUnavailable,
)


def settings_for():
    return Settings(
        _env_file=None,
        llm_enabled=True,
        integrations=[
            IntegrationInstance(
                id="model:openai:synthetic",
                kind="model",
                provider="openai",
                config={"model": "synthetic-model", "structured_output": True},
                secret_refs={"api_key": "GA_PLUGIN_OPENAI_SYNTHETIC"},
            )
        ],
        llm_consent=ProviderConsent(
            provider="openai",
            provider_instance_id="model:openai:synthetic",
            model="synthetic-model",
            categories={"health", "diary"},
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            policy_revision=1,
        ),
    )


def body(content=None, status="completed"):
    return {
        "id": "resp_synthetic",
        "status": status,
        "output": [
            {
                "type": "message",
                "content": content
                or [{"type": "output_text", "text": '{"urgent":false,"response_language":"en"}'}],
            }
        ],
        "usage": {"input_tokens": 8, "output_tokens": 3},
    }


@pytest.fixture
def transport(monkeypatch):
    requests = []
    responses = []

    def handle(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    original_client = httpx.Client
    monkeypatch.setattr(
        "garmin_ai.openai_adapter.httpx.Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setenv("GA_PLUGIN_OPENAI_SYNTHETIC", "synthetic-key")
    return requests, responses


def test_provider_uses_guarded_native_responses_and_no_storage(transport):
    requests, responses = transport
    responses.append(httpx.Response(200, json=body()))
    provider = create_model_provider(settings_for())
    result = provider.structured("Synthetic instruction", "Synthetic message", SafetyScreen)
    assert result == SafetyScreen(urgent=False, response_language="en")
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://api.openai.com/v1/responses"
    sent = json.loads(request.content)
    assert sent["model"] == "synthetic-model" and sent["store"] is False
    assert sent["text"]["format"]["strict"] is True
    assert sent["text"]["format"]["schema"]["additionalProperties"] is False
    assert sent["input"][1]["content"] == "Synthetic message"
    with pytest.raises(ProviderCapabilityUnsupported):
        provider.transcribe(b"synthetic", "audio/ogg")
    provider.close()


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (body([{"type": "refusal", "refusal": "synthetic"}]), ProviderRefused),
        (body(status="incomplete"), ProviderOutputTruncated),
        (body([{"type": "output_text", "text": '{"urgent":"yes"}'}]), ProviderOutputInvalid),
        ({"status": "completed", "output": []}, ProviderOutputInvalid),
    ],
)
def test_refusal_incomplete_and_invalid_output_never_become_results(transport, response, error):
    requests, responses = transport
    responses.append(httpx.Response(200, json=response))
    provider = create_model_provider(settings_for())
    with pytest.raises(error):
        provider.structured("Synthetic instruction", "Synthetic message", SafetyScreen)
    assert len(requests) == 1
    provider.close()


def test_rate_limit_does_not_retry(transport):
    requests, responses = transport
    responses.append(
        httpx.Response(
            429, headers={"retry-after": "180"}, json={"error": {"message": "synthetic"}}
        )
    )
    provider = create_model_provider(settings_for())
    with pytest.raises(ProviderRateLimited) as error:
        provider.structured("Synthetic instruction", "Synthetic message", SafetyScreen)
    assert error.value.retry_seconds >= 180
    assert len(requests) == 1
    provider.close()


def test_timeout_is_classified_without_retry(transport):
    requests, responses = transport
    responses.append(httpx.ReadTimeout("synthetic timeout"))
    provider = create_model_provider(settings_for())
    with pytest.raises(ProviderUnavailable):
        provider.structured("Synthetic instruction", "Synthetic message", SafetyScreen)
    assert len(requests) == 1
    provider.close()


def test_consent_revocation_and_untrusted_schema_stop_before_network(transport):
    requests, responses = transport
    settings = settings_for()
    provider = create_model_provider(settings)

    class UserSchema(BaseModel):
        synthetic_private_field: str

    with pytest.raises(ProviderCapabilityUnsupported):
        provider.structured("Synthetic", "Synthetic", UserSchema)
    settings.llm_consent.categories.clear()
    with pytest.raises(ProviderConsentRequired):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert not requests and not responses
    provider.close()


def test_missing_api_key_has_unavailable_status(monkeypatch):
    monkeypatch.delenv("GA_PLUGIN_OPENAI_SYNTHETIC", raising=False)
    status = integration_statuses(settings_for())[0]
    assert not status.available


def test_missing_secret_reference_and_undeclared_capability_are_not_available(transport):
    settings = settings_for()
    settings.integrations[0].secret_refs.clear()
    assert not integration_statuses(settings)[0].available
    settings.integrations[0].secret_refs = {"api_key": "GA_PLUGIN_OPENAI_SYNTHETIC"}
    settings.integrations[0].config.pop("structured_output")
    assert not integration_statuses(settings)[0].available


@pytest.mark.parametrize(
    ("status", "error"), [(401, ProviderAuthError), (503, ProviderUnavailable)]
)
def test_http_failures_are_classified_without_retries(transport, status, error):
    requests, responses = transport
    responses.append(httpx.Response(status, json={"error": {"message": "sensitive synthetic"}}))
    provider = create_model_provider(settings_for())
    with pytest.raises(error) as failure:
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert "sensitive synthetic" not in str(failure.value)
    assert len(requests) == 1
    provider.close()


def test_changed_model_configuration_cannot_reuse_old_client(transport):
    requests, _ = transport
    settings = settings_for()
    provider = create_model_provider(settings)
    settings.integrations[0].config["model"] = "new-synthetic-model"
    settings.llm_consent.model = "new-synthetic-model"
    with pytest.raises(ProviderConsentRequired):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert not requests
    provider.close()


def test_rotated_secret_cannot_be_used_through_old_client(transport, monkeypatch):
    requests, _ = transport
    provider = create_model_provider(settings_for())
    monkeypatch.setenv("GA_PLUGIN_OPENAI_SYNTHETIC", "synthetic-rotated-key")
    with pytest.raises(ProviderConsentRequired):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert not requests
    provider.close()


def test_persisted_consent_revocation_stops_openai_before_the_next_call(transport, db, db_engine):
    from garmin_ai.models import AppState

    requests, responses = transport
    responses.append(httpx.Response(200, json=body()))
    provider = create_model_provider(settings_for(), db_engine)
    assert not provider.structured("Synthetic", "Synthetic", SafetyScreen).urgent
    db.add(AppState(key="preferences:onboarding", value={"model_categories": []}))
    db.commit()
    with pytest.raises(ProviderConsentRequired):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert len(requests) == 1
    provider.close()


def test_unexpected_native_tool_calls_are_rejected(transport):
    requests, responses = transport
    responses.append(
        httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "function_call", "call_id": "synthetic-call", "name": "synthetic"}
                ],
            },
        )
    )
    provider = create_model_provider(settings_for())
    with pytest.raises(ProviderOutputInvalid):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert len(requests) == 1
    provider.close()


def test_logs_do_not_include_credentials_or_message_contents(transport, caplog):
    _, responses = transport
    responses.append(httpx.Response(200, json=body()))
    provider = create_model_provider(settings_for())
    with caplog.at_level("INFO", logger="garmin_ai"):
        provider.structured(
            "Synthetic private instruction", "Synthetic private message", SafetyScreen
        )
    assert "synthetic-key" not in caplog.text
    assert "Synthetic private" not in caplog.text
    provider.close()


def test_web_chat_uses_openai_through_actual_http_ingress(transport, db, db_engine):
    from uuid import uuid4

    from sqlalchemy import func, select

    from garmin_ai.agent import Interpretation
    from garmin_ai.api import create_app
    from garmin_ai.config import ApiToken
    from garmin_ai.models import Event

    requests, responses = transport
    command = Interpretation(
        intent="clarify", confidence=0.8, response_language="en", clarification="What time?"
    )
    responses.append(
        httpx.Response(200, json=body([{"type": "output_text", "text": command.model_dump_json()}]))
    )
    settings = settings_for()
    key = "synthetic-web-openai-" + "x" * 32
    settings.api_tokens = [ApiToken(key=key, scopes={"admin"})]
    db.commit()
    with TestClient(create_app(settings, db_engine)) as client:
        response = client.post(
            "/web-chat/messages",
            headers={"Authorization": "Bearer " + key},
            json={"client_message_id": str(uuid4()), "text": "A synthetic vague entry"},
        )
    assert response.status_code == 200
    assert "What time?" in response.json()["reply"]["text"]
    assert db.scalar(select(func.count()).select_from(Event)) == 0
    assert len(requests) == 1


def test_structured_planner_accepts_zero_or_multiple_bounded_calls(transport):
    from garmin_ai.agent import AgentStep, ReadCall

    requests, responses = transport
    plans = [
        AgentStep(answer="Synthetic answer", evidence_ids=[1]),
        AgentStep(
            calls=[
                ReadCall(name="health_snapshot", arguments_json='{"day":"2026-10-01"}'),
                ReadCall(name="health_snapshot", arguments_json='{"day":"2026-10-02"}'),
            ]
        ),
    ]
    for plan in plans:
        responses.append(
            httpx.Response(
                200, json=body([{"type": "output_text", "text": plan.model_dump_json()}])
            )
        )
    provider = create_model_provider(settings_for())
    try:
        for plan in plans:
            assert provider.structured("Synthetic", "Synthetic", AgentStep) == plan
    finally:
        provider.close()
    assert len(requests) == 2
