"""Synthetic plugin results and failures at the model contract boundary."""

import logging
import time
from datetime import UTC, datetime
from threading import Event as ThreadEvent

import pytest
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select

from garmin_ai.config import IntegrationInstance, ProviderConsent, Settings
from garmin_ai.integrations import (
    ConsentGuardedModel,
    IntegrationFactory,
    IntegrationRegistry,
    create_model_provider,
)
from garmin_ai.llm import (
    GeminiProvider,
    GeminiSchemaCompiler,
    JsonSchemaCompiler,
    ModelResult,
    ModelUsage,
    ProviderAuthError,
    ProviderCapabilityUnsupported,
    ProviderConsentRequired,
    ProviderCooldown,
    ProviderFallbackDeadline,
    ProviderModelUnavailable,
    ProviderOutputInvalid,
    ProviderOutputTruncated,
    ProviderRateLimited,
    ProviderRefused,
    ProviderRequestInvalid,
    ProviderUnavailable,
)
from garmin_ai.models import Audit, Event
from garmin_ai.natural_language import process_tracker_text
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)


class Result(BaseModel):
    score: int


class Plugin:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = 0

    def structured(self, _instruction, _prompt, _schema):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value

    def transcribe(self, _data, _mime_type):
        self.calls += 1
        return self.value

    def close(self):
        pass


class HttpFailure(Exception):
    def __init__(self, code):
        super().__init__("synthetic private payload")
        self.status_code = code


def model(
    plugin,
    *,
    capabilities=frozenset({"structured_output"}),
    engine=None,
    timeout=None,
    consent_categories=None,
):
    instance = IntegrationInstance(
        id="model:synthetic:first",
        kind="model",
        provider="synthetic",
        config={"model": "test-v1"},
        request_timeout_seconds=timeout,
    )
    settings = Settings(
        _env_file=None,
        llm_enabled=True,
        integrations=[instance],
        llm_consent=ProviderConsent(
            provider="synthetic",
            provider_instance_id=instance.id,
            model="test-v1",
            categories=consent_categories or {"health", "diary", "audio"},
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            policy_revision=1,
        ),
    )
    return ConsentGuardedModel(plugin, instance, settings, capabilities, engine)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (HttpFailure(429), ProviderRateLimited),
        (HttpFailure(401), ProviderAuthError),
        (HttpFailure(404), ProviderModelUnavailable),
        (HttpFailure(500), ProviderUnavailable),
        (HttpFailure(400), ProviderRequestInvalid),
        (TimeoutError("synthetic private payload"), ProviderUnavailable),
        (ProviderRefused("synthetic private payload"), ProviderRefused),
        (ProviderOutputTruncated("synthetic private payload"), ProviderOutputTruncated),
    ],
)
def test_failures_are_typed_and_do_not_expose_provider_payload(failure, expected, caplog):
    plugin = Plugin(error=failure)
    with caplog.at_level(logging.INFO, logger="garmin_ai"):
        with pytest.raises(expected) as raised:
            model(plugin).structured(
                "synthetic secret instruction", "synthetic private payload", Result
            )
    assert "synthetic private payload" not in str(raised.value)
    assert raised.value.__context__ is None
    assert "synthetic private payload" not in caplog.text
    assert "synthetic secret instruction" not in caplog.text
    assert plugin.calls == 1
    assert any(record.message == "model_plugin_request" for record in caplog.records)
    record = next(record for record in caplog.records if record.message == "model_plugin_request")
    assert record.outcome == "error"
    assert record.latency_ms >= 0


def test_structured_response_is_validated_before_returning_to_application():
    assert (
        model(Plugin(value={"score": 3})).structured("instruction", "synthetic", Result).score == 3
    )
    with pytest.raises(ProviderOutputInvalid):
        model(Plugin(value={"score": "not a number"})).structured(
            "instruction", "synthetic", Result
        )
    with pytest.raises(ProviderOutputInvalid) as raised:
        model(Plugin(value=Result.model_construct(score="not a number"))).structured(
            "instruction", "synthetic", Result
        )
    assert raised.value.__context__ is None


def test_unexpected_schema_validator_failure_does_not_expose_input():
    class BrokenValidator(BaseModel):
        value: str

        @model_validator(mode="after")
        def fail(self):
            raise RuntimeError("synthetic private payload")

    with pytest.raises(ProviderOutputInvalid) as raised:
        model(Plugin(value={"value": "synthetic private payload"})).structured(
            "instruction", "synthetic", BrokenValidator
        )
    assert "synthetic private payload" not in str(raised.value)
    assert raised.value.__context__ is None


def test_malformed_vendor_error_is_reported_without_its_payload():
    class BrokenError(Exception):
        @property
        def status_code(self):
            raise RuntimeError("synthetic private payload")

    with pytest.raises(ProviderUnavailable) as raised:
        model(Plugin(error=BrokenError("synthetic private payload"))).structured(
            "instruction", "synthetic", Result
        )
    assert "synthetic private payload" not in str(raised.value)
    assert raised.value.__context__ is None


def test_active_job_context_reaches_the_plugin_worker():
    from garmin_ai.provider_gate import ACTIVE_JOB

    class ContextPlugin(Plugin):
        def structured(self, *_args):
            assert ACTIVE_JOB.get() == "synthetic-job"
            return {"score": 3}

    token = ACTIVE_JOB.set("synthetic-job")
    try:
        assert model(ContextPlugin()).structured("instruction", "synthetic", Result).score == 3
    finally:
        ACTIVE_JOB.reset(token)


def test_schema_compilers_never_replace_domain_validation():
    class Constrained(BaseModel):
        identifier: str = Field(pattern="^valid$")

    original = JsonSchemaCompiler().compile(Constrained)
    reduced = GeminiSchemaCompiler().compile(Constrained)
    assert original["properties"]["identifier"]["pattern"] == "^valid$"
    assert "pattern" not in reduced["properties"]["identifier"]
    with pytest.raises(ProviderOutputInvalid):
        model(Plugin(value={"identifier": "invalid"})).structured(
            "instruction", "synthetic", Constrained
        )


def test_optional_usage_is_validated_and_logged_without_response_content(caplog):
    plugin = Plugin(
        value=ModelResult(value={"score": 3}, usage=ModelUsage(input_tokens=12, output_tokens=4))
    )
    with caplog.at_level(logging.INFO, logger="garmin_ai"):
        result = model(plugin).structured("private instruction", "private input", Result)
    assert result.score == 3
    record = next(record for record in caplog.records if record.message == "model_plugin_request")
    assert record.input_tokens == 12 and record.output_tokens == 4
    assert "private input" not in caplog.text and "private instruction" not in caplog.text
    invalid = ModelResult.model_construct(
        value={"score": 3}, usage={"input_tokens": "private input"}
    )
    with pytest.raises(ProviderOutputInvalid):
        model(Plugin(value=invalid)).structured("instruction", "synthetic", Result)


def test_structured_capability_is_checked_before_the_plugin_is_called():
    plugin = Plugin(value={"score": 3})
    with pytest.raises(ProviderCapabilityUnsupported):
        model(plugin, capabilities=frozenset()).structured("instruction", "synthetic", Result)
    assert plugin.calls == 0


def test_unsupported_audio_never_reaches_the_plugin():
    plugin = Plugin(value="synthetic")
    with pytest.raises(ProviderCapabilityUnsupported):
        model(plugin).transcribe(b"synthetic", "audio/ogg")
    assert plugin.calls == 0


def test_invalid_transcription_is_not_treated_as_a_success():
    plugin = Plugin(value={"unexpected": "synthetic"})
    with pytest.raises(ProviderOutputInvalid):
        model(plugin, capabilities=frozenset({"transcription"})).transcribe(
            b"synthetic", "audio/ogg"
        )
    assert plugin.calls == 1


def test_audio_only_consent_can_transcribe_but_cannot_send_diary_text(db, db_engine, monkeypatch):
    plugin = Plugin(value="synthetic transcript")

    class PluginConfig(BaseModel):
        model: str

    registry = IntegrationRegistry()
    registry.register(
        IntegrationFactory(
            kind="model",
            provider="synthetic",
            plugin_factory=lambda _context: plugin,
            config_model=PluginConfig,
            capabilities=frozenset({"structured_output", "transcription"}),
        )
    )
    monkeypatch.setattr("garmin_ai.integrations.default_registry", lambda _settings: registry)
    configured = model(plugin).settings
    consent = configured.llm_consent.model_copy(update={"categories": {"audio"}})
    configured = configured.model_copy(update={"llm_consent": consent})
    provider = create_model_provider(configured, db_engine)
    assert provider.transcribe(b"synthetic", "audio/ogg") == "synthetic transcript"
    with pytest.raises(ProviderConsentRequired):
        provider.structured("instruction", "private diary input", Result)
    assert plugin.calls == 1


def test_diary_only_model_request_does_not_require_health_consent(db, db_engine):
    from garmin_ai.models import AppState

    draft = TrackerSetupDraft(
        key="synthetic_rating",
        name="Synthetic rating",
        locale="en",
        fields=[TrackerFieldDraft(key="value", label="Rating", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    db.add(AppState(key="preferences:onboarding", value={"model_categories": ["diary"]}))
    db.commit()
    text = "Synthetic rating today at 14:00 value 3"
    quote = "today at 14:00"
    at, value_at = text.index(quote), text.rindex("3")
    plugin = Plugin(
        value={
            "schema_version": "tracker.nl.v1",
            "intent": "create_entry",
            "definition_version_id": created["action"]["definition_version_id"],
            "start": "2026-10-22T14:00:00Z",
            "start_evidence": {"start": at, "end": at + len(quote), "quote": quote},
            "fields": [
                {
                    "field_id": "user.synthetic_rating.value",
                    "value": 3,
                    "evidence": {"start": value_at, "end": value_at + 1, "quote": "3"},
                }
            ],
            "confidence": 1,
        }
    )
    provider = model(plugin, engine=db_engine, consent_categories={"diary"})
    result = process_tracker_text(
        db,
        provider,
        {"text": text, "operation_id": "diary-only-consent"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=datetime(2026, 10, 22, 14, tzinfo=UTC),
        timezone="UTC",
    )
    db.commit()
    assert result["written"] is True
    assert plugin.calls == 1
    counts = (
        db.scalar(select(func.count()).select_from(Event)),
        db.scalar(select(func.count()).select_from(Audit)),
    )
    plugin.error = HttpFailure(429)
    replay = process_tracker_text(
        db,
        provider,
        {"text": text, "operation_id": "diary-only-consent"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=datetime(2026, 10, 22, 14, tzinfo=UTC),
        timezone="UTC",
    )
    db.commit()
    assert replay == result
    assert plugin.calls == 1
    assert counts == (
        db.scalar(select(func.count()).select_from(Event)),
        db.scalar(select(func.count()).select_from(Audit)),
    )
    with pytest.raises(ProviderConsentRequired):
        provider.structured("instruction", "health data", Result)
    assert plugin.calls == 1


def test_gemini_uses_the_explicit_diary_category_without_a_live_call():
    consent = ProviderConsent(
        provider="gemini",
        provider_instance_id="model:gemini:primary",
        model="synthetic-model",
        categories={"diary"},
        granted_at=datetime(2026, 1, 1, tzinfo=UTC),
        policy_revision=1,
    )
    settings = Settings(
        _env_file=None, llm_enabled=True, gemini_model="synthetic-model", llm_consent=consent
    )
    provider = GeminiProvider.__new__(GeminiProvider)
    provider.settings = settings
    provider.model = "synthetic-model"
    provider.generation_config = {}
    seen = []

    def create(**kwargs):
        seen.append(kwargs["model_categories"])
        return Result(score=3)

    provider._create = create
    assert (
        provider.structured_for_categories("instruction", "synthetic", Result, {"diary"}).score == 3
    )
    assert seen == [{"diary"}]
    with pytest.raises(ProviderConsentRequired):
        provider.structured("instruction", "health data", Result)


def test_configured_gemini_deadline_and_diary_only_consent_are_honored(monkeypatch):
    from types import SimpleNamespace

    instance = IntegrationInstance(
        id="model:gemini:primary", kind="model", provider="gemini", request_timeout_seconds=0.2
    )
    consent = ProviderConsent(
        provider="gemini",
        provider_instance_id=instance.id,
        model="synthetic-model",
        categories={"diary"},
        granted_at=datetime(2026, 1, 1, tzinfo=UTC),
        policy_revision=1,
    )
    settings = Settings(
        _env_file=None,
        llm_enabled=True,
        gemini_api_key="synthetic",
        gemini_model="synthetic-model",
        integrations=[instance],
        llm_consent=consent,
    )
    monkeypatch.setattr(
        "garmin_ai.llm.genai.Client",
        lambda **_kwargs: SimpleNamespace(interactions=SimpleNamespace()),
    )
    provider = GeminiProvider(settings)
    seen = []

    def request(**kwargs):
        seen.append(kwargs["timeout"])
        return SimpleNamespace(output_text='{"score":3}')

    monkeypatch.setattr(provider, "_request", request)
    assert (
        provider.structured_for_categories("instruction", "synthetic", Result, {"diary"}).score == 3
    )
    assert 0 < seen[0] <= 0.2


def test_gemini_usage_and_latency_are_logged_without_request_content(caplog):
    from types import SimpleNamespace

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.request_gate = None
    provider._request = lambda **_kwargs: SimpleNamespace(
        output_text="synthetic private response",
        usage=SimpleNamespace(total_input_tokens=12, total_output_tokens=4),
    )
    with caplog.at_level(logging.INFO, logger="garmin_ai"):
        response = provider._create(input="synthetic private prompt")
    assert response.output_text == "synthetic private response"
    record = next(record for record in caplog.records if record.message == "model_provider_request")
    assert record.input_tokens == 12 and record.output_tokens == 4
    assert record.outcome == "success" and record.latency_ms >= 0
    assert "synthetic private" not in caplog.text


def test_oversized_audio_is_rejected_before_the_plugin_is_called():
    plugin = Plugin(value="synthetic transcript")
    with pytest.raises(ValueError, match="20 MB"):
        model(plugin, capabilities=frozenset({"transcription"})).transcribe(
            b"x" * (20 * 1024 * 1024 + 1), "audio/ogg"
        )
    assert plugin.calls == 0


def test_typed_rate_limit_retains_its_retry_delay_without_its_message():
    failure = ProviderRateLimited("synthetic private payload")
    failure.retry_seconds = 240
    with pytest.raises(ProviderRateLimited) as raised:
        model(Plugin(error=failure)).structured("instruction", "synthetic", Result)
    assert raised.value.retry_seconds == 240
    assert "synthetic private payload" not in str(raised.value)


def test_retry_after_is_honored_even_when_error_response_is_falsey():
    class Response:
        status_code = 429
        headers = {"Retry-After": "240"}

        def __bool__(self):
            return False

    error = RuntimeError("synthetic private payload")
    error.response = Response()
    with pytest.raises(ProviderRateLimited) as raised:
        model(Plugin(error=error)).structured("instruction", "synthetic", Result)
    assert raised.value.retry_seconds == 240


def test_raw_rate_limit_opens_durable_gate_before_another_call(db, db_engine):
    first = Plugin(error=HttpFailure(429))
    with pytest.raises(ProviderRateLimited):
        model(first, engine=db_engine).structured("instruction", "synthetic", Result)
    restarted = Plugin(value={"score": 3})
    with pytest.raises(ProviderCooldown):
        model(restarted, engine=db_engine).structured("instruction", "synthetic", Result)
    assert first.calls == 1
    assert restarted.calls == 0


def test_timeout_keeps_inflight_gate_locked_until_plugin_finishes(db, db_engine):
    class SlowPlugin(Plugin):
        def __init__(self):
            super().__init__()
            self.started = ThreadEvent()
            self.release = ThreadEvent()
            self.cancelled = ThreadEvent()

        def structured(self, *_args):
            self.calls += 1
            self.started.set()
            self.release.wait(5)
            return {"score": 3}

        def cancel(self):
            self.cancelled.set()

    plugin = SlowPlugin()
    first = model(
        plugin,
        capabilities=frozenset({"structured_output", "cancellation"}),
        engine=db_engine,
        timeout=0.3,
    )
    try:
        started = time.monotonic()
        with pytest.raises(ProviderFallbackDeadline):
            first.structured("instruction", "synthetic", Result)
        assert time.monotonic() - started < 2
        assert plugin.started.is_set() and plugin.cancelled.is_set()
        second_plugin = Plugin(value={"score": 4})
        second = model(second_plugin, engine=db_engine, timeout=0.3)
        with pytest.raises(ProviderCooldown):
            second.structured("instruction", "synthetic", Result)
        assert second_plugin.calls == 0
    finally:
        plugin.release.set()
        _wait_for_gate_release(db_engine, first.request_gate.lock_key)
    assert second.structured("instruction", "synthetic", Result).score == 4


def test_timed_out_gate_wait_does_not_send_a_late_request():
    class DelayedGate:
        def __init__(self):
            self.entered = ThreadEvent()
            self.release = ThreadEvent()
            self.finished = ThreadEvent()

        def call(self, request, **_kwargs):
            self.entered.set()
            self.release.wait(5)
            try:
                return request()
            finally:
                self.finished.set()

    plugin = Plugin(value={"score": 3})
    provider = model(plugin, timeout=0.1)
    gate = DelayedGate()
    provider.request_gate = gate
    try:
        with pytest.raises(ProviderFallbackDeadline):
            provider.structured("instruction", "synthetic", Result)
        assert gate.entered.is_set()
    finally:
        gate.release.set()
        assert gate.finished.wait(5)
    assert plugin.calls == 0


def _wait_for_gate_release(engine, lock_key):
    from sqlalchemy import text

    deadline = time.monotonic() + 5
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        while time.monotonic() < deadline:
            if connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}):
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                return
            time.sleep(0.01)
    pytest.fail("Synthetic provider did not release its gate")


def test_invalid_plugin_response_cannot_write_a_tracker_fact(db, db_engine):
    draft = TrackerSetupDraft(
        key="synthetic_rating",
        name="Synthetic rating",
        locale="en",
        fields=[TrackerFieldDraft(key="value", label="Rating", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    db.commit()
    before = (
        db.scalar(select(func.count()).select_from(Event)),
        db.scalar(select(func.count()).select_from(Audit)),
    )
    plugin = Plugin(value={"intent": "create_entry", "unexpected": "synthetic"})
    result = process_tracker_text(
        db,
        model(plugin, engine=db_engine),
        {"text": "Synthetic rating today at 14:00", "operation_id": "invalid-plugin-response"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=datetime(2026, 10, 22, 14, tzinfo=UTC),
    )
    db.commit()
    assert result["written"] is False
    assert plugin.calls == 1
    assert before == (
        db.scalar(select(func.count()).select_from(Event)),
        db.scalar(select(func.count()).select_from(Audit)),
    )


def test_late_valid_plugin_response_cannot_write_after_timeout(db, db_engine):
    draft = TrackerSetupDraft(
        key="synthetic_rating",
        name="Synthetic rating",
        locale="en",
        fields=[TrackerFieldDraft(key="value", label="Rating", kind="scale", minimum=1, maximum=5)],
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    db.commit()
    before = (
        db.scalar(select(func.count()).select_from(Event)),
        db.scalar(select(func.count()).select_from(Audit)),
    )
    text = "Synthetic rating today at 14:00 value 3"
    quote = "today at 14:00"
    at = text.index(quote)
    value_at = text.rindex("3")

    class SlowPlugin(Plugin):
        def __init__(self):
            super().__init__()
            self.release = ThreadEvent()

        def structured(self, _instruction, _prompt, _schema):
            self.calls += 1
            self.release.wait(5)
            return {
                "schema_version": "tracker.nl.v1",
                "intent": "create_entry",
                "definition_version_id": created["action"]["definition_version_id"],
                "start": "2026-10-22T14:00:00Z",
                "start_evidence": {"start": at, "end": at + len(quote), "quote": quote},
                "fields": [
                    {
                        "field_id": "user.synthetic_rating.value",
                        "value": 3,
                        "evidence": {"start": value_at, "end": value_at + 1, "quote": "3"},
                    }
                ],
                "confidence": 1,
            }

    plugin = SlowPlugin()
    provider = model(plugin, engine=db_engine, timeout=0.3)
    try:
        result = process_tracker_text(
            db,
            provider,
            {"text": text, "operation_id": "late-plugin-response"},
            granted={"read:diary", "write:diary"},
            actor="test",
            now=datetime(2026, 10, 22, 14, tzinfo=UTC),
            timezone="UTC",
        )
        db.commit()
        assert result["written"] is False
        assert plugin.calls == 1
    finally:
        plugin.release.set()
        _wait_for_gate_release(db_engine, provider.request_gate.lock_key)
    assert before == (
        db.scalar(select(func.count()).select_from(Event)),
        db.scalar(select(func.count()).select_from(Audit)),
    )
