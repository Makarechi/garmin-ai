"""Provider boundary; Gemini receives bounded context and never credentials."""

import base64
import importlib
import json
import math
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

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


class Provider(Protocol):
    def structured(self, instruction: str, prompt: str, schema: type[Result]) -> Result: ...
    def transcribe(self, data: bytes, mime_type: str) -> str: ...


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


class ProviderFallbackDeadline(ProviderUnavailable):
    retry_seconds = 1


class ProviderRateLimited(ProviderUnavailable):
    retry_seconds = 120


def _respect_retry_after(failure, cause):
    """Keep server backoff in the application gate rather than sleeping in the SDK."""
    response = getattr(cause, "response", None) or getattr(cause, "raw_response", None)
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
        self._authorize({"health", "diary"})
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
        deadline = time.monotonic() + 110

        def attempt_model(model=None, **request_kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderFallbackDeadline("Gemini model fallback deadline exceeded")
            attempt_kwargs = dict(request_kwargs)
            if model is not None:
                attempt_kwargs["model"] = model
            request_timeout = attempt_kwargs.get("timeout", 60)
            attempt_kwargs["timeout"] = min(request_timeout, max(1, int(remaining)))
            response = self._request(**attempt_kwargs)
            return _response_parser(response) if _response_parser else response

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
        self._authorize({"health", "diary"})

        def parse(response):
            try:
                return schema.model_validate_json(response.output_text)
            except (ValidationError, AttributeError, TypeError):
                raise ProviderOutputInvalid("Provider output failed domain validation") from None

        response = self._create(
            model_categories={"health", "diary"},
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
                "schema": gemini_schema(schema),
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
