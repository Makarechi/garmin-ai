"""Compare the wire projections of pinned model schemas with native Rust."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from garmin_ai.agent import AgentStep, Interpretation, SafetyScreen
from garmin_ai.anthropic_schema import AnthropicSchemaCompiler
from garmin_ai.extension_tck import ModelProbe
from garmin_ai.natural_language import TrackerExtraction
from garmin_ai.openai_schema import OpenAISchemaCompiler
from garmin_ai.structured_schema import UnsupportedSchema


class StaticSchema:
    def __init__(self, schema):
        self.schema = schema

    def model_json_schema(self):
        return self.schema


def closed(**properties):
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }


def optional_fields(n, *, make_required=False):
    fields = {
        f"{'z' if index % 2 else 'a'}_{index}": {
            "anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
            "default": None,
        }
        for index in range(n)
    }
    return {**closed(**fields), "required": list(fields) if make_required else []}


def cases():
    for schema in (AgentStep, Interpretation, SafetyScreen, TrackerExtraction, ModelProbe):
        yield schema.__name__, schema
    generated = {
        "property_order": closed(
            z={"type": "string", "description": "synthetic metadata", "maxLength": 10},
            anyOf={"const": True},
            a={"type": "number", "minimum": 18_446_744_073_709_551_615},
        ),
        "formats": closed(
            web={"type": "string", "format": "uri"},
            mail={"type": "string", "format": "email"},
            unknown={"type": "string", "format": "synthetic"},
        ),
        "union": closed(
            choice={
                "oneOf": [
                    closed(kind={"const": "first"}),
                    closed(kind={"const": "second"}),
                ],
                "discriminator": {"propertyName": "kind"},
            }
        ),
        "multi_ref_root": {
            "$ref": "#/$defs/First",
            "$defs": {
                "First": {"$ref": "#/$defs/Second"},
                "Second": closed(row={"type": "array", "items": {"type": "integer"}}),
            },
        },
        "missing_root": {"$ref": "#/$defs/Missing", "$defs": {}},
        "reference_cycle": {
            "$ref": "#/$defs/Loop",
            "$defs": {"Loop": {"$ref": "#/$defs/Loop"}},
        },
        "recursive_child": {
            **closed(node={"$ref": "#/$defs/Node"}),
            "$defs": {
                "Node": closed(children={"type": "array", "items": {"$ref": "#/$defs/Node"}})
            },
        },
        "disconnected_cycle": {
            **closed(main={"type": "string"}),
            "$defs": {
                "Left": closed(right={"$ref": "#/$defs/Right"}),
                "Right": closed(left={"$ref": "#/$defs/Left"}),
            },
        },
        "unsupported_ref": closed(value={"$ref": "https://example.invalid/schema"}),
        "open_object": {
            **closed(value={"type": "string"}),
            "additionalProperties": {"type": "string"},
        },
        "untyped": closed(value={"title": "Unknown"}),
        "bad_root": {"type": "array", "items": {"type": "integer"}},
        "untyped_array": closed(value={"type": "array"}),
        "positional_array": closed(value={"type": "array", "prefixItems": [{"type": "integer"}]}),
        "conditional": closed(value={"type": "integer", "if": {"minimum": 1}}),
        "unsupported_composition": closed(value={"type": "integer", "allOf": [{"maximum": 2}]}),
        "17_nullable": optional_fields(17),
        "40_nullable": optional_fields(40),
        "41_nullable": optional_fields(41),
        "17_required_nullable": optional_fields(17, make_required=True),
        "nested_nullable": {
            **closed(
                z={"$ref": "#/$defs/Ordered"},
                original={"anyOf": [{"type": "null"}, {"type": "string"}]},
            ),
            "$defs": {"Ordered": optional_fields(35)},
        },
    }
    for name, schema in generated.items():
        yield name, StaticSchema(schema)


def ordered_schema_maps(schema, path=""):
    for key in ("properties", "$defs"):
        if key in schema:
            fields = schema[key]
            pointer = f"{path}/{key}"
            yield (pointer, list(fields))
            for name, value in fields.items():
                encoded = name.replace("~", "~0").replace("/", "~1")
                yield from ordered_schema_maps(value, f"{pointer}/{encoded}")
    if "items" in schema:
        yield from ordered_schema_maps(schema["items"], f"{path}/items")
    for index, variant in enumerate(schema.get("anyOf", [])):
        yield from ordered_schema_maps(variant, f"{path}/anyOf/{index}")


def run(binary):
    compilers = {"openai": OpenAISchemaCompiler, "anthropic": AnthropicSchemaCompiler}
    with TemporaryDirectory() as directory:
        root = Path(directory)
        manifest = []
        for index, (name, schema) in enumerate(cases()):
            original = schema.model_json_schema()
            (root / f"schema-{index}.json").write_text(
                json.dumps(original, ensure_ascii=False), encoding="utf-8"
            )
            expected = {"name": name}
            for provider, compiler in compilers.items():
                try:
                    compiled = compiler().compile(schema)
                    expected[provider] = {
                        "schema": compiled,
                        "ordered_maps": list(ordered_schema_maps(compiled)),
                    }
                except UnsupportedSchema as exc:
                    expected[provider] = {"error": str(exc)}
            manifest.append(expected)
        assert all("schema" in case[provider] for case in manifest[:5] for provider in compilers)
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        subprocess.run(
            [binary, "--ignored"],
            check=True,
            env={**os.environ, "GA_TEST_MODEL_SCHEMAS": str(root)},
        )
        print(f"Matched {len(manifest) * len(compilers)} provider schema decisions and outputs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
