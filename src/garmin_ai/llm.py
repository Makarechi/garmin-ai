"""Provider boundary; Gemini receives bounded context and never credentials."""

import base64
import importlib
import json
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from garmin_ai.config import Settings
from garmin_ai.prompts import load_prompt


class _LazyGenAI:
    """Load the optional SDK only when a configured provider needs it."""

    def __getattr__(self, name):
        try:
            module = importlib.import_module("google.genai")
        except ImportError as exc:
            raise AttributeError(name) from exc
        globals()["genai"] = module
        return getattr(module, name)


genai = _LazyGenAI()

Result = TypeVar("Result", bound=BaseModel)


class ModelUsage(BaseModel):
    """Optional provider-reported usage, with no request or response content."""

    model_config = ConfigDict(extra="forbid", strict=True)
    input_tokens: int | None = Field(default=None, ge=0, le=1_000_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=1_000_000_000)


class ModelResult(BaseModel):
    """Optional adapter envelope; legacy adapters may still return the value directly."""

    model_config = ConfigDict(extra="forbid")
    value: Any = Field(repr=False)
    usage: ModelUsage | None = None


class Provider(Protocol):
    def structured(self, instruction: str, prompt: str, schema: type[Result]) -> Result: ...
    def transcribe(self, data: bytes, mime_type: str) -> str: ...


def structured_with_categories(provider, instruction, prompt, schema, categories):
    """Use a trusted caller's narrow categories where the provider supports them."""
    method = getattr(provider, "structured_for_categories", None)
    if callable(method):
        return method(instruction, prompt, schema, categories)
    return provider.structured(instruction, prompt, schema)


class ProviderUnavailable(RuntimeError):
    retry_seconds = 60


class ProviderConsentRequired(ProviderUnavailable):
    pass


class ProviderAuthError(ProviderUnavailable):
    retry_seconds = 1800


class ProviderModelUnavailable(ProviderUnavailable):
    retry_seconds = 1800


class ProviderCooldown(ProviderUnavailable):
    def __init__(self, reason, retry_seconds):
        super().__init__("Provider requests are temporarily paused")
        self.reason = reason
        self.retry_seconds = retry_seconds


class ProviderRequestInvalid(RuntimeError):
    """One invalid request; other provider work may continue."""


class ProviderCapabilityUnsupported(ProviderRequestInvalid):
    """A configured provider cannot perform this operation; retry cannot help."""


class ProviderOutputInvalid(RuntimeError):
    pass


class ProviderRefused(ProviderRequestInvalid):
    """The provider explicitly refused a request."""


class ProviderOutputTruncated(ProviderOutputInvalid):
    """The provider returned an incomplete response."""


class ProviderFallbackDeadline(ProviderUnavailable):
    retry_seconds = 1


class ProviderRateLimited(ProviderUnavailable):
    retry_seconds = 120


def normalize_plugin_error(error):
    """Classify an adapter failure without exposing upstream payloads or credentials."""
    if isinstance(error, ProviderCooldown):
        reason = (
            error.reason
            if isinstance(error.reason, str)
            and error.reason in {"busy", "quota", "auth", "model", "model_cooldown", "unavailable"}
            else "unavailable"
        )
        try:
            seconds = max(1, int(error.retry_seconds))
            datetime.now(UTC) + timedelta(seconds=seconds)
        except (TypeError, ValueError, OverflowError):
            seconds = 60
        return ProviderCooldown(reason, seconds)
    known = (
        (ProviderConsentRequired, ProviderConsentRequired, "Model consent is required"),
        (ProviderAuthError, ProviderAuthError, "Model authorization failed"),
        (ProviderRateLimited, ProviderRateLimited, "Model request is rate limited"),
        (ProviderModelUnavailable, ProviderModelUnavailable, "Configured model is unavailable"),
        (ProviderFallbackDeadline, ProviderFallbackDeadline, "Model request deadline exceeded"),
        (
            ProviderCapabilityUnsupported,
            ProviderCapabilityUnsupported,
            "Model capability is unsupported",
        ),
        (ProviderRefused, ProviderRefused, "Model refused the request"),
        (ProviderOutputTruncated, ProviderOutputTruncated, "Model response was incomplete"),
        (ProviderOutputInvalid, ProviderOutputInvalid, "Model output failed validation"),
        (ProviderRequestInvalid, ProviderRequestInvalid, "Model request was rejected"),
        (ProviderUnavailable, ProviderUnavailable, "Model request failed"),
    )
    for source, target, message in known:
        if isinstance(error, source):
            normalized = target(message)
            break
    else:
        response = getattr(error, "response", None)
        status = getattr(error, "status_code", None)
        if not isinstance(status, int):
            status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            status = getattr(error, "code", None)
        if status == 429:
            normalized = ProviderRateLimited("Model request is rate limited")
        elif status in (401, 403):
            normalized = ProviderAuthError("Model authorization failed")
        elif status == 404:
            normalized = ProviderModelUnavailable("Configured model is unavailable")
        elif (
            status == 408
            or (isinstance(status, int) and status >= 500)
            or isinstance(error, (httpx.TransportError, ConnectionError, TimeoutError))
        ):
            normalized = ProviderUnavailable("Model request failed")
        else:
            normalized = ProviderRequestInvalid("Model request was rejected")
    if isinstance(normalized, ProviderUnavailable):
        try:
            retry = float(getattr(error, "retry_seconds", 0))
            if math.isfinite(retry) and retry > 0:
                seconds = math.ceil(retry)
                datetime.now(UTC) + timedelta(seconds=seconds)
                normalized.retry_seconds = max(normalized.retry_seconds, seconds)
        except (TypeError, ValueError, OverflowError):
            pass
        return _respect_retry_after(normalized, error)
    return normalized


def _respect_retry_after(failure, cause):
    """Keep server backoff in the application gate rather than sleeping in the SDK."""
    response = getattr(cause, "response", None)
    if response is None:
        response = getattr(cause, "raw_response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return failure
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is None:
            return failure
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            deadline = parsedate_to_datetime(raw)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            seconds = (deadline - datetime.now(UTC)).total_seconds()
        if math.isfinite(seconds) and seconds > 0:
            delay = math.ceil(seconds)
            datetime.now(UTC) + timedelta(seconds=delay)  # Reject unrepresentable headers.
            failure.retry_seconds = max(failure.retry_seconds, delay)
    except (TypeError, ValueError, OverflowError, AttributeError):
        pass
    return failure


def gemini_schema(model: type[BaseModel]) -> dict:
    """Project Pydantic's richer schema onto Gemini's supported JSON subset.

    Domain validation still uses the original model, including every constraint.
    """
    original = model.model_json_schema()
    definitions = original.get("$defs", {})

    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return convert(definitions[node["$ref"].split("/")[-1]])
        result = {}
        for key, value in node.items():
            if key == "properties":
                result[key] = {name: convert(schema) for name, schema in value.items()}
            elif key in {"type", "description", "enum", "required", "items", "anyOf"}:
                result[key] = convert(value)
            elif key == "oneOf":
                result["anyOf"] = convert(value)
            elif key == "const":
                result["enum"] = [value]
        if "properties" in result:
            result["required"] = list(result["properties"])
        return result

    return convert(original)


class SchemaCompiler(Protocol):
    def compile(self, schema: type[BaseModel]) -> dict: ...


class JsonSchemaCompiler:
    def compile(self, schema: type[BaseModel]) -> dict:
        return schema.model_json_schema()


class GeminiSchemaCompiler:
    def compile(self, schema: type[BaseModel]) -> dict:
        return gemini_schema(schema)


class GeminiProvider:
    request_gate = None

    def __init__(self, settings: Settings, *, instance_id="model:gemini:primary"):
        if (
            not settings.llm_enabled
            or not settings.gemini_api_key.get_secret_value()
            or not settings.gemini_model
        ):
            raise ProviderUnavailable("Gemini is not configured")
        try:
            client_factory = genai.Client
        except AttributeError as exc:
            raise ProviderUnavailable(
                "Gemini integration is unavailable; install the 'gemini' extra"
            ) from exc
        self.request_gate = None
        self.model = settings.gemini_model
        self.settings = settings
        self.instance_id = instance_id
        self.request_deadline_seconds = next(
            (
                instance.request_timeout_seconds
                for instance in settings.integrations
                if instance.id == instance_id and instance.kind == "model"
            ),
            None,
        )
        self._authorize(set())
        self.generation_config = (
            {"thinking_level": settings.gemini_thinking_level}
            if settings.gemini_thinking_level
            else {}
        )
        self.client = client_factory(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options={"timeout": 60000, "retry_options": {"attempts": 1}},
        )
        try:
            # The SDK lazily imports its Interactions adapter. Prepare it at
            # startup so a cold import cannot consume the first question's budget.
            # This initializes local objects only; it sends no model request.
            self._prepare_interactions()
        except Exception:
            self.client.close()
            raise ProviderUnavailable("Gemini client preparation failed") from None

    def _authorize(self, categories, model=None):
        consent = self.settings.llm_consent
        model = model or self.model
        if (
            not self.settings.llm_enabled
            or consent is None
            or consent.provider != "gemini"
            or consent.provider_instance_id != getattr(self, "instance_id", "model:gemini:primary")
            or consent.model != self.settings.gemini_model
            or (model != consent.model and model not in consent.fallback_models)
            or self.settings.gemini_model != self.model
            or consent.granted_at > datetime.now(UTC)
            or not categories <= consent.categories
        ):
            raise ProviderConsentRequired(
                "External model consent is missing or does not cover this request"
            )

    def _model_chain(self, model_categories):
        if not hasattr(self, "settings"):
            return [None]
        self._authorize(model_categories)
        models = [self.model]
        if self.settings.gemini_fallback_enabled:
            consent = self.settings.llm_consent
            approved = set(consent.fallback_models) if consent is not None else set()
            models.extend(
                model
                for model in self.settings.gemini_fallback_models
                if model in approved and model not in models
            )
        return models

    def _create(self, *, model_categories=frozenset(), _response_parser=None, **kwargs):
        models = self._model_chain(model_categories)
        kwargs.pop("model", None)
        configured_deadline = getattr(self, "request_deadline_seconds", None)
        deadline = time.monotonic() + (
            configured_deadline if configured_deadline is not None else 110
        )

        def attempt_model(model=None, **request_kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderFallbackDeadline("Gemini model fallback deadline exceeded")
            attempt_kwargs = dict(request_kwargs)
            if model is not None:
                attempt_kwargs["model"] = model
            request_timeout = attempt_kwargs.get("timeout", 60)
            attempt_kwargs["timeout"] = min(
                request_timeout,
                max(0.001, remaining)
                if configured_deadline is not None
                else max(1, int(remaining)),
            )
            started = time.monotonic()
            response = None
            outcome = "error"
            try:
                response = self._request(**attempt_kwargs)
                result = _response_parser(response) if _response_parser else response
                outcome = "success"
                return result
            finally:
                try:
                    fields = {
                        "provider_instance_id": getattr(
                            self, "instance_id", "model:gemini:primary"
                        ),
                        "operation": "request",
                        "outcome": outcome,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                    }
                    usage = getattr(response, "usage", None)
                    for source, target in (
                        ("total_input_tokens", "input_tokens"),
                        ("total_output_tokens", "output_tokens"),
                    ):
                        value = getattr(usage, source, None)
                        if type(value) is int and 0 <= value <= 1_000_000_000:
                            fields[target] = value
                    logging.getLogger("garmin_ai").info("model_provider_request", extra=fields)
                except Exception:
                    pass

        if self.request_gate is not None:
            return self.request_gate.call(
                attempt_model,
                model_categories=model_categories,
                models=models,
                **kwargs,
            )
        failures = []
        for model in models:
            try:
                return attempt_model(model=model, **kwargs)
            except (ProviderConsentRequired, ProviderAuthError, ProviderCooldown):
                raise
            except ProviderRequestInvalid:
                if failures:
                    raise failures[0] from None
                raise
            except (
                ProviderRateLimited,
                ProviderModelUnavailable,
                ProviderUnavailable,
                ProviderOutputInvalid,
            ) as exc:
                failures.append(exc)
        if failures:
            raise next(
                (error for error in failures if isinstance(error, ProviderRateLimited)), failures[0]
            )
        raise ProviderUnavailable("Gemini has no authorized model")

    def _prepare_interactions(self):
        interactions = self.client.interactions
        # The pinned Interactions adapter treats the public attempt count
        # as a retry count. Disable that extra retry too: the application
        # owns fallback deadlines and durable cooldowns, including Retry-After.
        configuration = getattr(interactions, "sdk_configuration", None)
        retry = getattr(configuration, "retry_config", None)
        if retry is not None and hasattr(retry, "max_retries"):
            retry.max_retries = 0
        return interactions

    def _request(self, **kwargs):
        try:
            return self._prepare_interactions().create(**kwargs)
        except Exception as exc:
            if (
                getattr(exc, "status_code", None) == 429
                or getattr(exc, "code", None) == 429
                or type(exc).__name__ == "RateLimitError"
            ):
                raise _respect_retry_after(
                    ProviderRateLimited("Gemini quota exhausted; retry later"), exc
                ) from None
            code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            details = getattr(exc, "details", {})
            envelope = details.get("error", details) if isinstance(details, dict) else {}
            reasons = envelope.get("details", []) if isinstance(envelope, dict) else []
            invalid_key = isinstance(reasons, list) and any(
                isinstance(item, dict) and item.get("reason") == "API_KEY_INVALID"
                for item in reasons
            )
            if code in (401, 403) or invalid_key:
                raise _respect_retry_after(
                    ProviderAuthError("Gemini authorization failed"), exc
                ) from None
            if code == 404:
                raise _respect_retry_after(
                    ProviderModelUnavailable("Configured Gemini model is unavailable"), exc
                ) from None
            if (
                (isinstance(code, int) and (code == 408 or code >= 500))
                or isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError))
                or type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}
            ):
                raise _respect_retry_after(
                    ProviderUnavailable("Gemini request failed"), exc
                ) from None
            raise ProviderRequestInvalid("Gemini rejected this request") from None

    def structured(self, instruction: str, prompt: str, schema: type[Result]) -> Result:
        return self.structured_for_categories(instruction, prompt, schema, {"health", "diary"})

    def structured_for_categories(
        self, instruction: str, prompt: str, schema: type[Result], categories: set[str]
    ) -> Result:
        if not categories or not categories <= {"health", "diary"}:
            raise ValueError("Structured model categories must be health and/or diary")
        self._authorize(categories)

        def parse(response):
            try:
                return schema.model_validate_json(response.output_text)
            except (ValidationError, AttributeError, TypeError):
                raise ProviderOutputInvalid("Provider output failed domain validation") from None

        response = self._create(
            model_categories=categories,
            _response_parser=parse,
            model=self.model,
            system_instruction=instruction,
            input=prompt,
            store=False,
            generation_config=self.generation_config,
            timeout=60,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": GeminiSchemaCompiler().compile(schema),
            },
        )
        return response if isinstance(response, schema) else parse(response)

    def transcribe(self, data: bytes, mime_type: str) -> str:
        self._authorize({"audio"})
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("Voice message exceeds 20 MB")

        class Transcript(BaseModel):
            text: str

        def parse(response):
            try:
                return Transcript.model_validate_json(response.output_text).text
            except (ValidationError, AttributeError, TypeError):
                raise ProviderOutputInvalid("Provider output failed domain validation") from None

        response = self._create(
            model_categories={"audio"},
            _response_parser=parse,
            model=self.model,
            system_instruction=load_prompt("voice_transcribe"),
            input=[
                {"type": "audio", "mime_type": mime_type, "data": base64.b64encode(data).decode()}
            ],
            store=False,
            generation_config=self.generation_config,
            timeout=60,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": Transcript.model_json_schema(),
            },
        )
        return response if isinstance(response, str) else parse(response)

    def close(self):
        self.client.close()


def compact(value, limit=24000):
    content = json.dumps(value, ensure_ascii=False, default=str)
    if len(content) > limit:
        return json.dumps(
            {
                "error": "result_too_large",
                "instruction": "Narrow the date range or use an analysis tool. Do not treat this as empty data.",
            }
        )
    return content
