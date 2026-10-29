"""Model diagnostics distinguish failures without logging private content."""

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from garmin_ai.llm import GeminiProvider, ProviderRateLimited, ProviderUnavailable
from garmin_ai.runtime import SafeFormatter


class SyntheticStatusError(Exception):
    def __init__(self, status):
        super().__init__("fictional private provider content")
        self.status_code = status


class APIConnectionError(Exception):
    pass


@pytest.mark.parametrize(
    ("failure", "expected_type", "kind", "status"),
    [
        (SyntheticStatusError(503), ProviderUnavailable, "upstream_unavailable", 503),
        (SyntheticStatusError(408), ProviderUnavailable, "timeout", 408),
        (
            httpx.ConnectError("fictional private provider content"),
            ProviderUnavailable,
            "network",
            None,
        ),
        (
            APIConnectionError("fictional private provider content"),
            ProviderUnavailable,
            "network",
            None,
        ),
        (
            httpx.ReadTimeout("fictional private provider content"),
            ProviderUnavailable,
            "timeout",
            None,
        ),
        (SyntheticStatusError(429), ProviderRateLimited, "rate_limited", 429),
    ],
)
def test_gemini_failure_logs_safe_diagnostics(caplog, failure, expected_type, kind, status):
    def fail(**_kwargs):
        raise failure

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.request_gate = None
    provider.client = SimpleNamespace(interactions=SimpleNamespace(create=fail))
    with caplog.at_level(logging.INFO, logger="garmin_ai"), pytest.raises(expected_type):
        provider._create(input="fictional private prompt")
    record = next(r for r in caplog.records if r.message == "model_provider_request")
    logged = json.loads(SafeFormatter().format(record))
    assert logged["outcome"] == "error"
    assert logged["failure_kind"] == kind
    assert logged.get("upstream_status") == status
    assert isinstance(logged["latency_ms"], int)
    assert "fictional private" not in json.dumps(logged)


def test_formatter_rejects_untrusted_model_metadata():
    record = logging.makeLogRecord(
        {
            "name": "garmin_ai",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "model_provider_request",
            "failure_kind": "fictional private content",
            "outcome": "fictional private content",
            "latency_ms": "fictional private content",
            "upstream_status": 123456789,
            "input_tokens": "fictional private content",
            "provider_instance_id": "fictional private content",
            "private_input": "fictional private content",
        }
    )
    logged = SafeFormatter().format(record)
    assert "fictional private content" not in logged
    assert "123456789" not in logged


def test_formatter_keeps_bounded_usage_without_the_response(caplog):
    provider = GeminiProvider.__new__(GeminiProvider)
    provider.request_gate = None
    provider._request = lambda **_kwargs: SimpleNamespace(
        output_text="fictional private response",
        usage=SimpleNamespace(total_input_tokens=12, total_output_tokens=4),
    )
    with caplog.at_level(logging.INFO, logger="garmin_ai"):
        provider._create(input="fictional private prompt")
    record = next(r for r in caplog.records if r.message == "model_provider_request")
    logged = json.loads(SafeFormatter().format(record))
    assert logged["outcome"] == "success"
    assert logged["provider_instance_id"] == "model:gemini:primary"
    assert logged["input_tokens"] == 12 and logged["output_tokens"] == 4
    assert "fictional private" not in json.dumps(logged)
