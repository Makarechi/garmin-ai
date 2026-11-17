"""Compare native model response validation against two pinned mock transports."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr

from garmin_ai import anthropic_adapter, openai_adapter
from garmin_ai.anthropic_schema import AnthropicSchemaCompiler
from garmin_ai.llm import ProviderOutputInvalid, ProviderRefused, ProviderUnavailable
from garmin_ai.openai_schema import OpenAISchemaCompiler

LABEL = "synthetic-provider-private"
PAYLOAD = {
    "choice": "Café ☕",
    "score": 97,
    "reference": "00000000-0000-0000-0000-000000000042",
    "observed_at": "2026-11-17T12:00:00Z",
}


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: str = Field(min_length=1, max_length=20)
    score: int = Field(ge=0, le=100)
    reference: UUID
    observed_at: AwareDatetime


def envelope(provider, payload=None, *, encoded=None):
    raw = encoded if encoded is not None else json.dumps(payload or PAYLOAD, ensure_ascii=False)
    if provider == "openai":
        return {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [LABEL]},
                {"type": "message", "content": [{"type": "output_text", "text": raw}]},
            ],
        }
    return {
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": LABEL},
            {"type": "redacted_thinking", "data": LABEL},
            {"type": "text", "text": raw},
        ],
    }


def variants(provider):
    def render(value):
        return json.dumps(value, ensure_ascii=False).encode()

    standard = envelope(provider)
    yield "success", render(standard)
    yield (
        "usage",
        render({**standard, "usage": {"input_tokens": 17, "output_tokens": 1_000_000_000}}),
    )
    yield "usage_missing", render({**standard, "usage": {"cache_read_input_tokens": 0}})
    yield "usage_wrong_shape", render({**standard, "usage": "ignored"})
    for invalid in [True, -1, 1_000_000_001, 3.0, "7"]:
        yield "strict_usage", render({**standard, "usage": {"input_tokens": invalid}})
    yield "schema", render(envelope(provider, {**PAYLOAD, "score": 101}))
    yield "schema_extra", render(envelope(provider, {**PAYLOAD, "unexpected": LABEL}))
    yield "invalid_reference", render(envelope(provider, {**PAYLOAD, "reference": "not-a-uuid"}))
    yield "invalid_instant", render(envelope(provider, {**PAYLOAD, "observed_at": "not-a-date"}))
    yield (
        "naive_instant",
        render(envelope(provider, {**PAYLOAD, "observed_at": "2026-11-17T12:00:00"})),
    )
    yield (
        "impossible_instant",
        render(envelope(provider, {**PAYLOAD, "observed_at": "2026-02-30T12:00:00Z"})),
    )
    yield "schema_missing", render(envelope(provider, {"score": 1}))
    yield "payload_not_json", render(envelope(provider, encoded=f"{LABEL}: not a document"))
    yield "payload_array", render(envelope(provider, encoded="[]"))
    yield "bad_utf8", b'{"secret":"\xff"}'
    yield "invalid_root", b"[]"
    yield "too_large", render({**standard, "padding": "x" * 1_048_576})
    yield "utf8_bom", b"\xef\xbb\xbf" + render(standard)
    whole = render(standard).decode()
    for encoding in ["utf-16", "utf-16-be", "utf-32", "utf-32-le"]:
        yield encoding, whole.encode(encoding)

    if provider == "openai":
        block = {"type": "output_text", "text": json.dumps(PAYLOAD)}

        def message(content):
            return [{"type": "message", "content": content}]

        yield "incomplete", render({**standard, "status": "incomplete"})
        yield "failed", render({**standard, "status": "failed"})
        yield (
            "refusal",
            render({**standard, "output": message([{"type": "refusal", "refusal": LABEL}])}),
        )
        yield (
            "refusal_after_repetition",
            render(
                {
                    **standard,
                    "output": message([block, block, {"type": "refusal", "refusal": LABEL}]),
                }
            ),
        )
        yield "multiple_outputs", render({**standard, "output": message([block, block])})
        yield (
            "wrong_output",
            render({**standard, "output": [{"type": "function_call", "name": LABEL}]}),
        )
        yield "empty", render({**standard, "output": message([])})
    else:
        block = {"type": "text", "text": json.dumps(PAYLOAD)}
        yield "incomplete", render({**standard, "stop_reason": "max_tokens"})
        yield "failed", render({**standard, "stop_reason": "stop_sequence"})
        yield (
            "refusal",
            render(
                {**standard, "stop_reason": "refusal", "content": [{"type": "text", "text": LABEL}]}
            ),
        )
        yield (
            "refusal_over_incomplete",
            render(
                {
                    **standard,
                    "stop_reason": "max_tokens",
                    "stop_details": {"type": "refusal", "detail": LABEL},
                }
            ),
        )
        yield "multiple_outputs", render({**standard, "content": [block, block]})
        yield "wrong_output", render({**standard, "content": [{"type": "tool_use", "name": LABEL}]})
        yield "empty", render({**standard, "content": []})


def adapters():
    openai_adapter.trusted_model_schema = lambda schema: schema is Reply
    anthropic_adapter.trusted_model_schema = lambda schema: schema is Reply
    key = SecretStr("synthetic-model-key-for-local-mock")
    return {
        "openai": openai_adapter.OpenAIProvider(
            openai_adapter.OpenAIConfig(model="synthetic", structured_output=True), key
        ),
        "anthropic": anthropic_adapter.AnthropicProvider(
            anthropic_adapter.AnthropicConfig(model="synthetic", structured_output=True), key
        ),
    }


def run(binary):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        manifest = {
            "schemas": {
                "application": Reply.model_json_schema(),
                "openai": OpenAISchemaCompiler().compile(Reply),
                "anthropic": AnthropicSchemaCompiler().compile(Reply),
            },
            "cases": [],
        }
        for provider, adapter in adapters().items():
            adapter.close()
            for name, body in variants(provider):
                with httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request, response=body: httpx.Response(200, content=response)
                    )
                ) as client:
                    adapter.client = client
                    try:
                        result = adapter.structured("Synthetic instruction", LABEL, Reply)
                        expected = {
                            "value": result.value.model_dump(mode="json"),
                            "usage": result.usage.model_dump(mode="json") if result.usage else None,
                        }
                    except (ProviderOutputInvalid, ProviderRefused, ProviderUnavailable) as exc:
                        assert LABEL not in str(exc)
                        expected = {"error": type(exc).__name__}
                index = len(manifest["cases"])
                (root / f"case-{index}.json").write_bytes(body)
                manifest["cases"].append({"provider": provider, "case": name, "expected": expected})
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        subprocess.run(
            [binary, "--ignored"],
            check=True,
            env={**os.environ, "GA_TEST_MODEL_RESPONSES": str(root)},
        )
        print(f"Matched {len(manifest['cases'])} Python adapter responses using local mocks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
