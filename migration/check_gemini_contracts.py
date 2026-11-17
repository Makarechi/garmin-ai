"""Compare native Gemini schema and response handling with the pinned Python stack."""

import argparse
import json
from pathlib import Path

from check_model_schemas import cases, ordered_schema_maps
from google.genai._gaos.types.interactions.interaction import Interaction
from pydantic import BaseModel

from garmin_ai.agent import AgentStep, Interpretation, SafetyScreen
from garmin_ai.extension_tck import ModelProbe
from garmin_ai.llm import (
    GeminiProvider,
    GeminiSchemaCompiler,
    ProviderOutputInvalid,
    ProviderOutputTruncated,
    ProviderUnavailable,
)
from garmin_ai.natural_language import TrackerExtraction


class Transcript(BaseModel):
    text: str


def block(text):
    return {"type": "text", "text": text}


def output(*contents):
    return {"type": "model_output", "content": list(contents)}


def interaction(*steps, status="completed", **metadata):
    return {"status": status, "steps": list(steps), **metadata}


def response_cases():
    probe = output(block('{"urgent": true}'))
    yield "normal", ModelProbe, interaction(probe)
    yield "false", ModelProbe, interaction(output(block('{"urgent": false}')))
    yield (
        "usage",
        ModelProbe,
        interaction(
            probe, usage={"total_input_tokens": 3, "total_output_tokens": 5, "total_tokens": 8}
        ),
    )
    yield (
        "unbounded_usage",
        ModelProbe,
        interaction(probe, usage={"total_input_tokens": 1000000001, "total_output_tokens": 0}),
    )
    yield "text_blocks", ModelProbe, interaction(output(block('{"urgent"'), block(": true}")))
    yield (
        "multiple_steps",
        ModelProbe,
        interaction(output(block('{"urgent"')), output(block(": true}"))),
    )
    cut = {"type": "user_input", "content": [block("synthetic prompt")]}
    yield "earlier_turn", ModelProbe, interaction(output(block("discard")), cut, probe)
    yield (
        "earlier_text_ignored",
        ModelProbe,
        interaction(
            output(
                block("discard"),
                {"type": "image", "mime_type": "image/png", "data": "AQI="},
                block('{"urgent": true}'),
            )
        ),
    )
    yield (
        "image_after_text",
        ModelProbe,
        interaction(
            output(
                block('{"urgent": true}'),
                {"type": "image", "mime_type": "image/png", "data": "AQI="},
            )
        ),
    )
    yield (
        "interruption_mid_json",
        ModelProbe,
        interaction(output(block('{"urgent"')), cut, output(block(": true}"))),
    )
    yield "no_forged_text", ModelProbe, interaction(output_text='{"urgent": true}')
    yield "invalid_json", ModelProbe, interaction(output(block("{no}")))
    yield "invalid_application", ModelProbe, interaction(output(block('{"urgent": "not-a-bool"}')))
    yield "unknown_field", ModelProbe, interaction(output(block('{"urgent": true, "unknown": 1}')))
    yield "optional_application_key", SafetyScreen, interaction(output(block('{"urgent": false}')))
    yield (
        "safe_language",
        SafetyScreen,
        interaction(output(block('{"urgent": false, "response_language":"en"}'))),
    )
    yield (
        "agent_calls_cap",
        AgentStep,
        interaction(
            output(block(json.dumps({"calls": [{"tool": "daily", "arguments_json": "{}"}] * 5})))
        ),
    )
    yield "agent_defaults", AgentStep, interaction(output(block('{"answer":"synthetic answer"}')))
    yield (
        "interpretation_bounds",
        Interpretation,
        interaction(output(block('{"intent":"undo","confidence":2}'))),
    )
    yield (
        "interpretation_defaults",
        Interpretation,
        interaction(output(block('{"intent":"undo","confidence":1}'))),
    )
    yield (
        "tracker_version",
        TrackerExtraction,
        interaction(output(block('{"schema_version":"unknown","intent":"none","confidence":0.5}'))),
    )
    yield "transcript", Transcript, interaction(output(block('{"text":"synthetic café ☕"}')))
    yield "malformed_transcript", Transcript, interaction(output(block('{"text":3}')))
    for status in [
        "incomplete",
        "budget_exceeded",
        "failed",
        "cancelled",
        "in_progress",
        "requires_action",
        "queued",
    ]:
        yield status, ModelProbe, interaction(probe, status=status)


def expected_result(schema, wire):
    result = Interaction.model_validate(wire)
    provider = object.__new__(GeminiProvider)
    provider._authorize = lambda *_args: None
    provider.model = "synthetic-gemini"
    provider.generation_config = {}
    provider._create = lambda **kwargs: kwargs["_response_parser"](result)
    try:
        if schema is Transcript:
            value = {"text": provider.transcribe(b"synthetic", "audio/ogg")}
        else:
            value = provider.structured_for_categories(
                "Synthetic instruction", "Synthetic input", schema, {"diary"}
            ).model_dump(mode="json", exclude_unset=True)
    except ProviderOutputTruncated:
        return {"error": "Incomplete"}
    except ProviderOutputInvalid:
        return {"error": "InvalidOutput"}
    except ProviderUnavailable:
        return {"error": "Unavailable"}
    usage = None
    if result.usage is not None:
        usage = []
        for kind in ("total_input_tokens", "total_output_tokens"):
            count = getattr(result.usage, kind, None)
            usage.append(count if type(count) is int and 0 <= count <= 1000000000 else None)
    raw = json.loads(result.output_text)
    assert raw == value, "Fixture compares original JSON before application normalization"
    return {"value": raw, "usage": usage}


def record(path):
    schemas = []
    for name, schema in cases():
        if name in {"bad_root", "unsupported_ref"}:
            continue
        original = schema.model_json_schema()
        try:
            wire = GeminiSchemaCompiler().compile(schema)
        except (KeyError, RecursionError):
            expected = {"error": True}
        else:
            expected = {"wire": wire, "ordered_maps": list(ordered_schema_maps(wire))}
        schemas.append({"name": name, "original": original, **expected})
    replies = [
        {
            "name": name,
            "original": schema.model_json_schema(),
            "wire": wire,
            **expected_result(schema, wire),
        }
        for name, schema, wire in response_cases()
    ]
    path.write_text(
        json.dumps({"schemas": schemas, "replies": replies}, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"Recorded {len(schemas)} schema and {len(replies)} response cases from the pinned Gemini stack"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    record(parser.parse_args().output)
