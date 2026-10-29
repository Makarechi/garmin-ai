from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, ValidationError

from garmin_ai.agent import AgentStep, Interpretation, SafetyScreen
from garmin_ai.anthropic_schema import AnthropicSchemaCompiler, UnsupportedSchema
from garmin_ai.natural_language import TrackerExtraction


@pytest.mark.parametrize("schema", [AgentStep, Interpretation, SafetyScreen, TrackerExtraction])
def test_application_models_compile_to_supported_closed_objects(schema):
    result = AnthropicSchemaCompiler().compile(schema)
    Draft202012Validator.check_schema(result)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            assert not set(node) & {
                "minimum",
                "maximum",
                "exclusiveMinimum",
                "exclusiveMaximum",
                "multipleOf",
                "minItems",
                "maxItems",
                "pattern",
                "oneOf",
                "discriminator",
            }
            for key, value in node.items():
                if key in {"properties", "$defs"}:
                    for child in value.values():
                        walk(child)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(result)


def test_original_model_still_rejects_values_outside_its_bounds():
    class Bounded(BaseModel):
        count: int = Field(ge=1)

    result = AnthropicSchemaCompiler().compile(Bounded)
    Draft202012Validator(result).validate({"count": 0})
    with pytest.raises(ValidationError):
        Bounded.model_validate({"count": 0})


def test_recursive_schema_is_rejected():
    class Node(BaseModel):
        children: list["Node"]

    class Root(BaseModel):
        tree: Node

    with pytest.raises(UnsupportedSchema, match="Recursive"):
        AnthropicSchemaCompiler().compile(Root)


def test_open_and_untyped_values_are_rejected():
    class Open(BaseModel):
        fields: dict[str, str]

    class Untyped(BaseModel):
        data: Any

    for schema in [Open, Untyped]:
        with pytest.raises(UnsupportedSchema):
            AnthropicSchemaCompiler().compile(schema)


@pytest.mark.parametrize(
    "value",
    [
        AgentStep(response_language="en"),
        Interpretation(intent="question", confidence=0.9, response_language="en"),
        SafetyScreen(urgent=False, response_language="en"),
        TrackerExtraction(schema_version="tracker.nl.v1", intent="clarify", confidence=0.9),
    ],
)
def test_sample_outputs_fit_transport_and_original_model(value):
    data = value.model_dump(mode="json")
    Draft202012Validator(AnthropicSchemaCompiler().compile(type(value))).validate(data)
    assert type(value).model_validate(data) == value
