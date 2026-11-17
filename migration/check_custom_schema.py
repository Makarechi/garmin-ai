"""Generate synthetic custom schema decisions from the pinned Python validator."""

import argparse
import json
from pathlib import Path

from garmin_ai.definitions import validate_schema

DIALECT = "https://json-schema.org/draft/2020-12/schema"
LOCAL = "#/$defs/"
BOUNDED = {"type": "integer", "minimum": 0, "maximum": 10}


def root(properties=None, **overrides):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties or {},
    } | overrides


def examples():
    yield {}
    yield root()
    yield root({"value": BOUNDED}, required=["value"])
    yield root({"value": BOUNDED}, required=["value", "value"])
    yield root({"value": BOUNDED}, required=["missing"])
    for key in ["value", "value_2", "A", "1abc", "x-y", "x" * 63, "x" * 64]:
        yield root({key: BOUNDED})
    for choice in [None, DIALECT, DIALECT + "#", "https://example.invalid/schema", True]:
        yield root(**{"$schema": choice})
    for variant in [
        {"type": "string"},
        {"type": "string", "maxLength": 1},
        {"type": "string", "maxLength": 2, "minLength": 1, "const": "🧪"},
        {"type": "string", "maxLength": 1, "const": "xy"},
        {"type": "string", "maxLength": 16_000},
        {"type": "string", "maxLength": 16_001},
        {"type": "integer", "enum": [1, 3, 4]},
        {"type": "integer", "enum": [True]},
        {"type": "number", "minimum": 0, "maximum": 0, "enum": [0, 1]},
        {"type": "array", "items": BOUNDED, "minItems": 0, "maxItems": 1000},
        {"type": "array", "items": BOUNDED, "minItems": 1000, "maxItems": 999},
        {"type": "array", "maxItems": 3},
        {"type": "boolean", "minimum": 4},
        {"type": "number", "minimum": True, "maximum": 1},
        {"type": "object", "properties": {}},
        {"type": "object", "additionalProperties": False},
        {"type": "object", "additionalProperties": False, "items": BOUNDED},
        {"const": {"bounded": [False, 2, None]}},
        {"enum": [[]]},
        {"enum": list(range(50))},
        {"enum": list(range(51))},
        {"enum": []},
        {"anyOf": [{"type": "null"}, BOUNDED]},
        {"oneOf": [{"type": "boolean", "title": "a"}, {"type": "boolean", "title": "b"}]},
        {"oneOf": [{"const": 4}, {"enum": [4.0]}]},
        {"oneOf": [{"const": 4}, {"enum": [5]}]},
        {"oneOf": [{"const": False}, {"enum": [0]}]},
        {"oneOf": []},
        {"type": "null", "description": "a" * 501},
        {"type": "null", "default": None},
        {"type": "null", "$schema": DIALECT},
        {"anyOf": [{"const": False}] * 5},
    ]:
        yield root({"value": variant})
    for integer in (True, False):
        for minimum in (-1, 0, 0.01, 1.01, 1.9, 2.0):
            for maximum in (0, 0.99, 1, 1.01, 2):
                for strict in (False, True):
                    yield root(
                        {
                            "value": {
                                "type": "integer" if integer else "number",
                                "exclusiveMinimum" if strict else "minimum": minimum,
                                "maximum": maximum,
                            }
                        }
                    )
    for name in ("Target", "bad/path", "", "bad space", "missing"):
        yield root({"value": {"$ref": LOCAL + name}}, **{"$defs": {"Target": BOUNDED}})
    yield root({"value": {"$ref": "file:///synthetic"}})
    yield root({"value": {"$ref": "https://example.invalid/synthetic"}})
    yield root({"value": {"$ref": LOCAL + "A"}}, **{"$defs": {"A": {"$ref": LOCAL + "A"}}})
    for depth in range(2, 12):
        names = [f"Node{i}" for i in range(depth)]
        chain = {
            name: {"$ref": LOCAL + following}
            for name, following in zip(names[:-1], names[1:], strict=True)
        }
        chain[names[-1]] = BOUNDED
        yield root({"value": {"$ref": LOCAL + names[0]}}, **{"$defs": chain})
        chain = {**chain, names[-1]: {"$ref": LOCAL + names[0]}}
        yield root({"value": {"$ref": LOCAL + names[0]}}, **{"$defs": chain})
    for depth in range(4, 10):
        nested = BOUNDED
        for _ in range(depth):
            nested = {"type": "array", "items": nested, "maxItems": 1}
        yield root({"value": nested})
    for count in range(4, 8):
        yield root({f"value{i}": BOUNDED | {"description": "🧪" * 500} for i in range(count)})


def allowed(schema):
    try:
        validate_schema(schema)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    corpus = [[schema, allowed(schema)] for schema in examples()]
    args.output.write_text(json.dumps(corpus, ensure_ascii=True, allow_nan=False), encoding="utf-8")
