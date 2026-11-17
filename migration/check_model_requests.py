"""Compare all approved native request bodies with pinned mock HTTP requests."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from check_model_schemas import ordered_schema_maps
from generate_model_schemas import APPROVED
from pydantic import SecretStr

from garmin_ai.anthropic_adapter import AnthropicConfig, AnthropicProvider
from garmin_ai.openai_adapter import OpenAIConfig, OpenAIProvider


def expected(adapter, schema_path):
    captured = []

    def observe(request):
        captured.append(json.loads(request.content))
        return httpx.Response(418, content=b"synthetic test status")

    adapter.close()
    values = {}
    with httpx.Client(transport=httpx.MockTransport(observe)) as client:
        adapter.client = client
        for name, schema in APPROVED.items():
            try:
                adapter.structured("Synthetic instruction", "synthetic tracker label ☕", schema)
            except httpx.HTTPStatusError as exc:
                assert exc.response.status_code == 418
            assert len(captured) == 1
            body = captured.pop()
            schema = body
            for part in schema_path:
                schema = schema[part]
            values[name] = {"body": body, "ordered_maps": list(ordered_schema_maps(schema))}
    return values


def run(binary):
    key = SecretStr("synthetic-credential-for-mock-requests")
    settings = {"model": "synthetic:model-v2", "structured_output": True, "max_output_tokens": 8192}
    documents = {
        "openai": expected(
            OpenAIProvider(OpenAIConfig(**settings), key), ("text", "format", "schema")
        ),
        "anthropic": expected(
            AnthropicProvider(AnthropicConfig(**settings), key),
            ("output_config", "format", "schema"),
        ),
    }
    with TemporaryDirectory() as directory:
        payload = Path(directory) / "requests.json"
        payload.write_text(json.dumps(documents, ensure_ascii=False), encoding="utf-8")
        subprocess.run(
            [binary, "--ignored"],
            check=True,
            env={**os.environ, "GA_TEST_MODEL_REQUESTS": str(payload)},
        )
    print("Matched ten approved provider request bodies against pinned HTTP mocks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
