"""Record requests through the pinned Gemini SDK with an in-process HTTP transport."""

import argparse
import json
from pathlib import Path

import httpx
from check_model_schemas import ordered_schema_maps
from generate_model_schemas import APPROVED
from google import genai

from garmin_ai.llm import GeminiProvider, ProviderOutputInvalid

MODEL = "synthetic-gemini-2"
KEY = "synthetic-key-for-local-mock"
AUDIO = bytes([0, 127, 128, 255, 42])


def record(path):
    captured = []

    def observe(request):
        assert request.method == "POST"
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.url.path == "/v1beta/interactions"
        assert request.headers["x-goog-api-key"] == KEY
        assert not request.url.query
        body = json.loads(request.content)
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": '{"urgent":true,"text":"synthetic"}'}],
                    },
                ],
            },
        )

    def capture(call):
        try:
            call()
        except ProviderOutputInvalid:
            # Returning mismatched structured text is intentional. The request still traversed
            # the SDK with the real response parser and no network transport.
            pass
        assert len(captured) == 1
        return captured.pop()

    cases = []
    with httpx.Client(transport=httpx.MockTransport(observe), trust_env=False) as http:
        with genai.Client(api_key=KEY, http_options={"httpx_client": http}) as client:
            provider = object.__new__(GeminiProvider)
            provider.client = client
            provider.model = MODEL
            provider._authorize = lambda *_args, **_kwargs: None
            provider._model_chain = lambda _categories: [MODEL]
            for level in ["", "low"]:
                provider.generation_config = {"thinking_level": level} if level else {}
                for name, schema in APPROVED.items():
                    wire = capture(
                        lambda schema=schema: provider.structured_for_categories(
                            "Synthetic instruction",
                            "Synthetic prompt ☕",
                            schema,
                            {"diary"},
                        )
                    )
                    cases.append(
                        {
                            "name": name,
                            "level": level,
                            "wire": wire,
                            "ordered_maps": list(
                                ordered_schema_maps(wire["response_format"]["schema"])
                            ),
                        }
                    )
                voice = capture(lambda: provider.transcribe(AUDIO, "audio/ogg"))
                cases.append(
                    {
                        "name": "transcript",
                        "level": level,
                        "wire": voice,
                        "ordered_maps": list(
                            ordered_schema_maps(voice["response_format"]["schema"])
                        ),
                    }
                )
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    print(f"Captured {len(cases)} Gemini text and voice requests using HTTP mocks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    record(parser.parse_args().output)
