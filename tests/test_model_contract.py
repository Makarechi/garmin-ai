"""Synthetic plugin results and failures at the model contract boundary."""

import logging
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

from garmin_ai.config import IntegrationInstance, ProviderConsent, Settings
from garmin_ai.integrations import ConsentGuardedModel
from garmin_ai.llm import (
    ProviderAuthError,
    ProviderCapabilityUnsupported,
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


def model(plugin, *, capabilities=frozenset({"structured_output"}), engine=None):
    instance = IntegrationInstance(
        id="model:synthetic:first", kind="model", provider="synthetic", config={"model": "test-v1"}
    )
    settings = Settings(
        _env_file=None,
        llm_enabled=True,
        integrations=[instance],
        llm_consent=ProviderConsent(
            provider="synthetic",
            provider_instance_id=instance.id,
            model="test-v1",
            categories={"health", "diary", "audio"},
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
    from garmin_ai.llm import ProviderCooldown

    with pytest.raises(ProviderCooldown):
        model(restarted, engine=db_engine).structured("instruction", "synthetic", Result)
    assert first.calls == 1
    assert restarted.calls == 0


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
