from typing import Literal

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from garmin_ai.agent import AgentStep, Interpretation, SafetyScreen
from garmin_ai.natural_language import TrackerExtraction
from garmin_ai.openai_schema import OpenAISchemaCompiler, UnsupportedSchema


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["one", "two"]
    count: int = Field(ge=1, default=1)
    note: str | None = None


def test_strict_schema_requires_all_fields_and_keeps_server_validation():
    result = OpenAISchemaCompiler().compile(Sample)
    assert set(result["required"]) == {"category", "count", "note"}
    assert result["additionalProperties"] is False
    Draft202012Validator.check_schema(result)
    Draft202012Validator(result).validate({"category": "one", "count": 1, "note": None})
    with pytest.raises(ValidationError):
        Sample.model_validate({"category": "one", "count": 0, "note": None})


@pytest.mark.parametrize("schema", [AgentStep, Interpretation, SafetyScreen, TrackerExtraction])
def test_application_schemas_compile_to_closed_strict_objects(schema):
    result = OpenAISchemaCompiler().compile(schema)
    Draft202012Validator.check_schema(result)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            assert "oneOf" not in node and "discriminator" not in node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(result)


def test_open_mapping_is_rejected_instead_of_silently_losing_fields():
    class OpenMapping(BaseModel):
        values: dict[str, str]

    with pytest.raises(UnsupportedSchema):
        OpenAISchemaCompiler().compile(OpenMapping)


def test_untyped_values_are_rejected_before_an_external_request():
    from typing import Any

    class Untyped(BaseModel):
        value: Any

    with pytest.raises(UnsupportedSchema):
        OpenAISchemaCompiler().compile(Untyped)


def test_schema_description_and_default_literals_are_not_sent():
    class PrivateDescription(BaseModel):
        value: str = Field(default="private-example", description="private-description")

    result = str(OpenAISchemaCompiler().compile(PrivateDescription))
    assert "private-example" not in result
    assert "private-description" not in result


@pytest.mark.parametrize(
    "value",
    [
        AgentStep(response_language="en"),
        Interpretation(intent="question", confidence=0.9, response_language="en"),
        SafetyScreen(urgent=False, response_language="en"),
        TrackerExtraction(schema_version="tracker.nl.v1", intent="clarify", confidence=0.9),
    ],
)
def test_representative_application_outputs_match_transport_and_domain_schema(value):
    wire = value.model_dump(mode="json")
    Draft202012Validator(OpenAISchemaCompiler().compile(type(value))).validate(wire)
    assert type(value).model_validate(wire) == value
