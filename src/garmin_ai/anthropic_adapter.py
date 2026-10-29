"""Native Anthropic Messages adapter for trusted structured-output schemas."""

import json
from typing import Literal

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from garmin_ai.anthropic_schema import AnthropicSchemaCompiler, UnsupportedSchema
from garmin_ai.llm import (
    ModelResult,
    ModelUsage,
    ProviderCapabilityUnsupported,
    ProviderOutputInvalid,
    ProviderOutputTruncated,
    ProviderRefused,
)
from garmin_ai.model_schema_policy import trusted_model_schema

_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_MAX_RESPONSE_BYTES = 1024 * 1024
_API_VERSION = "2026-10-21"


class AnthropicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=200)
    structured_output: Literal[True]
    timeout_seconds: float = Field(default=60, ge=1, le=120)
    max_output_tokens: int = Field(default=4096, ge=256, le=8192)


class AnthropicProvider:
    def __init__(self, config: AnthropicConfig, api_key: SecretStr):
        self.config = config
        self._api_key = api_key
        self.client = httpx.Client(timeout=config.timeout_seconds, follow_redirects=False)

    def structured(self, instruction, prompt, schema):
        if not trusted_model_schema(schema):
            raise ProviderCapabilityUnsupported(
                "This response schema is not approved for Anthropic"
            )
        try:
            wire_schema = AnthropicSchemaCompiler().compile(schema)
        except UnsupportedSchema:
            raise ProviderCapabilityUnsupported("This response schema is not supported") from None
        request = {
            "model": self.config.model,
            "max_tokens": self.config.max_output_tokens,
            "system": instruction,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": wire_schema}},
        }
        with self.client.stream(
            "POST",
            _MESSAGES_URL,
            headers={
                "Authorization": "Bearer " + self._api_key.get_secret_value(),
                "anthropic-version": _API_VERSION,
            },
            json=request,
        ) as response:
            response.raise_for_status()
            chunks = bytearray()
            for part in response.iter_bytes():
                chunks.extend(part)
                if len(chunks) > _MAX_RESPONSE_BYTES:
                    raise ProviderOutputInvalid("Model response exceeds the size limit")
        try:
            envelope = json.loads(chunks)
        except (ValueError, UnicodeDecodeError):
            raise ProviderOutputInvalid("Model response is not valid JSON") from None
        if not isinstance(envelope, dict):
            raise ProviderOutputInvalid("Model response has an invalid envelope")
        reason = envelope.get("stop_reason")
        details = envelope.get("stop_details")
        if reason == "refusal" or isinstance(details, dict) and details.get("type") == "refusal":
            raise ProviderRefused("Model refused the request")
        if reason == "max_tokens":
            raise ProviderOutputTruncated("Model response was incomplete")
        if reason != "end_turn":
            raise ProviderOutputInvalid("Model response has an unsupported stop reason")
        content = envelope.get("content")
        if not isinstance(content, list):
            raise ProviderOutputInvalid("Model response has invalid content")
        texts = []
        for block in content:
            if not isinstance(block, dict):
                raise ProviderOutputInvalid("Model response has invalid content")
            if block.get("type") in {"thinking", "redacted_thinking"}:
                continue
            if block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise ProviderOutputInvalid("Model response has unsupported content")
            texts.append(block["text"])
        if len(texts) != 1:
            raise ProviderOutputInvalid("Model response must contain one structured output")
        try:
            payload = json.loads(texts[0])
            Draft202012Validator(wire_schema).validate(payload)
            value = schema.model_validate(payload)
            raw_usage = envelope.get("usage")
            usage = (
                ModelUsage.model_validate(
                    {
                        "input_tokens": raw_usage.get("input_tokens"),
                        "output_tokens": raw_usage.get("output_tokens"),
                    }
                )
                if isinstance(raw_usage, dict)
                else None
            )
        except (ValidationError, SchemaValidationError, ValueError, TypeError):
            raise ProviderOutputInvalid("Model output failed validation") from None
        return ModelResult(value=value, usage=usage)

    def close(self):
        self.client.close()


def create_anthropic_provider(context):
    key = context.secrets.get("api_key")
    if key is None:
        raise ValueError("Anthropic API key reference is required")
    return AnthropicProvider(context.config, key)
