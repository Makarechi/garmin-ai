"""Synthetic Claude Messages API and shared model policy checks."""

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
                id="model:anthropic:synthetic",
                kind="model",
                provider="anthropic",
                config={"model": "synthetic-model", "structured_output": True},
                secret_refs={"api_key": "GA_PLUGIN_ANTHROPIC_SYNTHETIC"},
            )
        ],
        llm_consent=ProviderConsent(
            provider="anthropic",
            provider_instance_id="model:anthropic:synthetic",
            model="synthetic-model",
            categories={"health", "diary"},
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            policy_revision=1,
        ),
    )


def body(content=None, reason="end_turn"):
    return {
        "id": "msg_synthetic",
        "type": "message",
        "role": "assistant",
        "stop_reason": reason,
        "content": content
        if content is not None
        else [{"type": "text", "text": '{"urgent":false,"response_language":"en"}'}],
        "usage": {"input_tokens": 8, "output_tokens": 3},
    }


def interpretation_response(value):
    from garmin_ai.anthropic_schema import AnthropicSchemaCompiler

    schema = AnthropicSchemaCompiler().compile(type(value))
    result = value.model_dump(mode="json")

    def omit_optional_nulls(obj, definition):
        for key in definition["properties"]:
            if key not in definition["required"] and obj.get(key) is None:
                obj.pop(key, None)

    omit_optional_nulls(result, schema)
    for event in result["events"]:
        omit_optional_nulls(event, schema["$defs"]["EventInput"])
        if event["payload"]["type"] == "caffeine":
            omit_optional_nulls(event["payload"], schema["$defs"]["Caffeine"])
    return result


@pytest.fixture
def transport(monkeypatch):
    requests, responses = [], []

    def handle(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    original_client = httpx.Client
    monkeypatch.setattr(
        "garmin_ai.anthropic_adapter.httpx.Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setenv("GA_PLUGIN_ANTHROPIC_SYNTHETIC", "synthetic-key")
    return requests, responses


def test_guarded_messages_request_and_text_output(transport):
    requests, responses = transport
    responses.append(httpx.Response(200, json=body()))
    provider = create_model_provider(settings_for())
    result = provider.structured("Synthetic instruction", "Synthetic message", SafetyScreen)
    assert result == SafetyScreen(urgent=False, response_language="en")
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["anthropic-version"] == "2026-10-21"
    sent = json.loads(request.content)
    assert sent["model"] == "synthetic-model"
    assert sent["system"] == "Synthetic instruction"
    assert sent["messages"] == [{"role": "user", "content": "Synthetic message"}]
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["format"]["schema"]["additionalProperties"] is False
    with pytest.raises(ProviderCapabilityUnsupported):
        provider.transcribe(b"synthetic", "audio/ogg")
    provider.close()


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (body(reason="refusal"), ProviderRefused),
        ({**body(), "stop_details": {"type": "refusal"}}, ProviderRefused),
        (body(reason="max_tokens"), ProviderOutputTruncated),
        (body(reason="tool_use"), ProviderOutputInvalid),
        (body([{"type": "text", "text": '{"urgent":"yes"}'}]), ProviderOutputInvalid),
        (body([]), ProviderOutputInvalid),
        (
            body([{"type": "tool_use", "id": "synthetic-call", "name": "synthetic", "input": {}}]),
            ProviderOutputInvalid,
        ),
    ],
)
def test_incomplete_refused_or_unexpected_output_is_rejected(transport, response, error):
    requests, responses = transport
    responses.append(httpx.Response(200, json=response))
    provider = create_model_provider(settings_for())
    with pytest.raises(error):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert len(requests) == 1
    provider.close()


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, ProviderAuthError), (429, ProviderRateLimited), (529, ProviderUnavailable)],
)
def test_http_failures_are_classified_without_retries(transport, status, error):
    requests, responses = transport
    responses.append(
        httpx.Response(
            status,
            headers={"retry-after": "180"},
            json={"error": {"message": "sensitive synthetic"}},
        )
    )
    provider = create_model_provider(settings_for())
    with pytest.raises(error) as failure:
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert "sensitive synthetic" not in str(failure.value)
    assert len(requests) == 1
    if status == 429:
        assert failure.value.retry_seconds >= 180
    provider.close()


def test_timeout_does_not_retry(transport):
    requests, responses = transport
    responses.append(httpx.ReadTimeout("synthetic timeout"))
    provider = create_model_provider(settings_for())
    with pytest.raises(ProviderUnavailable):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert len(requests) == 1
    provider.close()


def test_untrusted_schema_and_revoked_consent_stop_before_network(transport):
    requests, _ = transport
    settings = settings_for()
    provider = create_model_provider(settings)

    class UserSchema(BaseModel):
        synthetic_private_field: str

    with pytest.raises(ProviderCapabilityUnsupported):
        provider.structured("Synthetic", "Synthetic", UserSchema)
    settings.llm_consent.categories.clear()
    with pytest.raises(ProviderConsentRequired):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert not requests
    provider.close()


def test_configuration_and_secret_rotation_stop_old_clients(transport, monkeypatch):
    requests, _ = transport
    settings = settings_for()
    provider = create_model_provider(settings)
    settings.integrations[0].config["model"] = "new-synthetic-model"
    settings.llm_consent.model = "new-synthetic-model"
    with pytest.raises(ProviderConsentRequired):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    provider.close()
    settings = settings_for()
    provider = create_model_provider(settings)
    monkeypatch.setenv("GA_PLUGIN_ANTHROPIC_SYNTHETIC", "synthetic-rotated-key")
    with pytest.raises(ProviderConsentRequired):
        provider.structured("Synthetic", "Synthetic", SafetyScreen)
    assert not requests
    provider.close()


def test_missing_api_key_or_capability_is_not_available(monkeypatch):
    monkeypatch.delenv("GA_PLUGIN_ANTHROPIC_SYNTHETIC", raising=False)
    assert not integration_statuses(settings_for())[0].available
    monkeypatch.setenv("GA_PLUGIN_ANTHROPIC_SYNTHETIC", "synthetic-key")
    settings = settings_for()
    settings.integrations[0].config.pop("structured_output")
    assert not integration_statuses(settings)[0].available


def test_persisted_consent_revocation_stops_before_next_request(transport, db, db_engine):
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


def test_original_domain_limits_are_enforced_after_anthropic_schema_projection(transport):
    from garmin_ai.agent import Interpretation

    requests, responses = transport
    invalid = interpretation_response(Interpretation(intent="question", confidence=0.5))
    invalid["confidence"] = 2.0
    from jsonschema import Draft202012Validator

    from garmin_ai.anthropic_schema import AnthropicSchemaCompiler

    Draft202012Validator(AnthropicSchemaCompiler().compile(Interpretation)).validate(invalid)
    responses.append(
        httpx.Response(200, json=body([{"type": "text", "text": json.dumps(invalid)}]))
    )
    provider = create_model_provider(settings_for())
    with pytest.raises(ProviderOutputInvalid):
        provider.structured("Synthetic", "Synthetic", Interpretation)
    assert len(requests) == 1
    provider.close()


def test_web_chat_uses_anthropic_through_actual_http_ingress(transport, db, db_engine):
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
        httpx.Response(
            200, json=body([{"type": "text", "text": json.dumps(interpretation_response(command))}])
        )
    )
    settings = settings_for()
    key = "synthetic-web-anthropic-" + "x" * 32
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


def test_diary_interpretation_with_omitted_unknowns_round_trips(transport):
    from garmin_ai.agent import Interpretation

    requests, responses = transport
    command = Interpretation(
        intent="log",
        confidence=0.9,
        events=[
            {"start": "2026-01-01T10:00:00Z", "payload": {"type": "caffeine", "beverage": "coffee"}}
        ],
    )
    response = interpretation_response(command)
    assert "target_event_id" not in response
    assert "end" not in response["events"][0]
    responses.append(
        httpx.Response(200, json=body([{"type": "text", "text": json.dumps(response)}]))
    )
    provider = create_model_provider(settings_for())
    try:
        assert provider.structured("Synthetic", "Synthetic coffee", Interpretation) == command
    finally:
        provider.close()
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
            httpx.Response(200, json=body([{"type": "text", "text": plan.model_dump_json()}]))
        )
    provider = create_model_provider(settings_for())
    try:
        for plan in plans:
            assert provider.structured("Synthetic", "Synthetic", AgentStep) == plan
    finally:
        provider.close()
    assert len(requests) == 2


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
