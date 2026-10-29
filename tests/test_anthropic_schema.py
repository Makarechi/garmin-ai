from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, ValidationError, create_model

from garmin_ai.agent import AgentStep, Interpretation, SafetyScreen
from garmin_ai.anthropic_schema import AnthropicSchemaCompiler, UnsupportedSchema
from garmin_ai.natural_language import TrackerExtraction
from garmin_ai.openai_schema import OpenAISchemaCompiler


@pytest.mark.parametrize("schema", [AgentStep, Interpretation, SafetyScreen, TrackerExtraction])
def test_application_models_compile_to_supported_closed_objects(schema):
    result = AnthropicSchemaCompiler().compile(schema)
    Draft202012Validator.check_schema(result)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) <= set(node["properties"])
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

    def complexity(node):
        if isinstance(node, list):
            counts = [complexity(item) for item in node]
            return sum(count[0] for count in counts), sum(count[1] for count in counts)
        if not isinstance(node, dict):
            return (0, 0)
        unions = int("anyOf" in node)
        optional = len(node.get("properties", {})) - len(node.get("required", []))
        for key, value in node.items():
            children = value.values() if key in {"properties", "$defs"} else [value]
            for child in children:
                child_unions, child_optional = complexity(child)
                unions += child_unions
                optional += child_optional
        return unions, optional

    unions, optional = complexity(result)
    assert unions <= 16
    assert optional <= 24


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


def test_direct_recursive_model_is_supported_only_by_openai():
    class Node(BaseModel):
        children: list["Node"]

    compiled = OpenAISchemaCompiler().compile(Node)
    assert compiled["type"] == "object"
    Draft202012Validator(compiled).validate({"children": [{"children": []}]})
    with pytest.raises(UnsupportedSchema, match="Recursive"):
        AnthropicSchemaCompiler().compile(Node)


@pytest.mark.parametrize("compiler", [OpenAISchemaCompiler, AnthropicSchemaCompiler])
def test_fixed_tuple_with_open_mapping_is_rejected(compiler):
    class Positional(BaseModel):
        pair: tuple[dict[str, str], str]

    with pytest.raises(UnsupportedSchema):
        compiler().compile(Positional)

    class FixedTypes(BaseModel):
        pair: tuple[int, str]

    with pytest.raises(UnsupportedSchema):
        compiler().compile(FixedTypes)


def test_open_and_untyped_values_are_rejected():
    class Open(BaseModel):
        fields: dict[str, str]

    class Untyped(BaseModel):
        data: Any

    for schema in [Open, Untyped]:
        with pytest.raises(UnsupportedSchema):
            AnthropicSchemaCompiler().compile(schema)


def test_unrepresentable_complexity_is_rejected():
    required_unions = create_model(
        "RequiredUnions", **{f"field_{i}": (int | str, ...) for i in range(17)}
    )
    too_many_optional = create_model(
        "TooManyOptional", **{f"field_{i}": (int | None, None) for i in range(41)}
    )
    for schema in (required_unions, too_many_optional):
        with pytest.raises(UnsupportedSchema, match="complexity"):
            AnthropicSchemaCompiler().compile(schema)


def omit_optional_nulls(data, schema, definitions):
    if "$ref" in schema:
        return omit_optional_nulls(data, definitions[schema["$ref"][8:]], definitions)
    if "anyOf" in schema:
        for choice in schema["anyOf"]:
            candidate = omit_optional_nulls(data, choice, definitions)
            if Draft202012Validator({**choice, "$defs": definitions}).is_valid(candidate):
                return candidate
        return data
    if isinstance(data, dict) and "properties" in schema:
        return {
            key: omit_optional_nulls(value, schema["properties"][key], definitions)
            for key, value in data.items()
            if value is not None or key in schema["required"]
        }
    if isinstance(data, list) and "items" in schema:
        return [omit_optional_nulls(value, schema["items"], definitions) for value in data]
    return data


@pytest.mark.parametrize(
    "value",
    [
        AgentStep(response_language="en"),
        Interpretation(intent="question", confidence=0.9, response_language="en"),
        SafetyScreen(urgent=False, response_language="en"),
        TrackerExtraction(schema_version="tracker.nl.v1", intent="clarify", confidence=0.9),
        Interpretation(
            intent="log",
            confidence=0.9,
            events=[
                {
                    "start": "2026-01-01T10:00:00Z",
                    "payload": {"type": "caffeine", "beverage": "coffee"},
                }
            ],
        ),
    ],
)
def test_sample_outputs_fit_transport_and_original_model(value):
    data = value.model_dump(mode="json")
    schema = AnthropicSchemaCompiler().compile(type(value))
    data = omit_optional_nulls(data, schema, schema.get("$defs", {}))
    Draft202012Validator(schema).validate(data)
    assert type(value).model_validate(data) == value
