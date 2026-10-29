"""Minimal native Responses API adapter for trusted structured-output schemas."""

import json
from typing import Literal

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from garmin_ai.llm import (
    ModelResult,
    ModelUsage,
    ProviderCapabilityUnsupported,
    ProviderOutputInvalid,
    ProviderOutputTruncated,
    ProviderRefused,
    ProviderUnavailable,
)
from garmin_ai.model_schema_policy import trusted_model_schema
from garmin_ai.openai_schema import OpenAISchemaCompiler, UnsupportedSchema

_RESPONSES_URL = "https://api.openai.com/v1/responses"
_MAX_RESPONSE_BYTES = 1024 * 1024


class OpenAIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=200)
    structured_output: Literal[True]
    timeout_seconds: float = Field(default=60, ge=1, le=120)
    max_output_tokens: int = Field(default=4096, ge=256, le=8192)


class OpenAIProvider:
    def __init__(self, config: OpenAIConfig, api_key: SecretStr):
        self.config = config
        self._api_key = api_key
        self.client = httpx.Client(timeout=config.timeout_seconds, follow_redirects=False)

    def structured(self, instruction, prompt, schema):
        if not trusted_model_schema(schema):
            raise ProviderCapabilityUnsupported("This response schema is not approved for OpenAI")
        try:
            wire_schema = OpenAISchemaCompiler().compile(schema)
        except UnsupportedSchema:
            raise ProviderCapabilityUnsupported("This response schema is not supported") from None
        request = {
            "model": self.config.model,
            "store": False,
            "max_output_tokens": self.config.max_output_tokens,
            "input": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": prompt},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "application_response",
                    "strict": True,
                    "schema": wire_schema,
                }
            },
        }
        with self.client.stream(
            "POST",
            _RESPONSES_URL,
            headers={"Authorization": "Bearer " + self._api_key.get_secret_value()},
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
        status = envelope.get("status")
        if status == "incomplete":
            raise ProviderOutputTruncated("Model response was incomplete")
        if status != "completed":
            raise ProviderUnavailable("Model response did not complete")
        output = envelope.get("output")
        if not isinstance(output, list):
            raise ProviderOutputInvalid("Model response has no output")
        texts = []
        for item in output:
            if not isinstance(item, dict):
                raise ProviderOutputInvalid("Model response has invalid output")
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or not isinstance(item.get("content"), list):
                raise ProviderOutputInvalid("Model response has unsupported output")
            for content in item["content"]:
                if not isinstance(content, dict):
                    raise ProviderOutputInvalid("Model response has invalid content")
                if content.get("type") == "refusal":
                    raise ProviderRefused("Model refused the request")
                if content.get("type") != "output_text" or not isinstance(content.get("text"), str):
                    raise ProviderOutputInvalid("Model response has unsupported content")
                texts.append(content["text"])
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


def create_openai_provider(context):
    key = context.secrets.get("api_key")
    if key is None:
        raise ValueError("OpenAI API key reference is required")
    return OpenAIProvider(context.config, key)
