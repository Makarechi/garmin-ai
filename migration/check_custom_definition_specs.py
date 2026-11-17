"""Generate pinned Python decisions for complete synthetic custom definitions."""

import argparse
import copy
import json
from pathlib import Path

from garmin_ai.definitions import DefinitionSpec, contract_hash

BASE = {
    "key": "user.synthetic_counter",
    "labels": {"EN": "Synthetic 🧪", "zh-hant-tw": "Sample"},
    "topology": "point",
    "schema": {
        "type": "object",
        "properties": {"sample": {"type": "integer", "minimum": 0, "maximum": 10}},
        "required": ["sample"],
        "additionalProperties": False,
    },
    "fields": {
        "sample": {
            "id": "user.synthetic_counter.sample",
            "labels": {"en-GB": "Sample"},
            "semantic": "count",
        }
    },
}


def examples():
    yield BASE
    for tag in [
        "EN",
        "en-GB",
        "az-latn-AZ",
        "en-gb-OED",
        "i-Klingon",
        "en-A-aaa-B-bbb",
        "en-A-aa-a-bb",
        "en-Latn-US-x-Test",
        "en-Latn-US-x",
        "X-Test-Private",
        "en-123",
        "en-QQQ-Qx-LA",
        "en-abc-def-ghi-Latn-US",
        "en-abc-def-ghi-jkl-Latn-US",
        "en-sample-sample",
        "en-1996-1996",
        "en-9-aa",
        "e",
        "x",
        "e_En",
        "",
        "en--GB",
    ]:
        yield BASE | {"labels": {tag: "Synthetic"}}
    for label in ["", " ", "\x1f", "🧪" * 120, "🧪" * 121, 5, None]:
        yield BASE | {"labels": {"en": label}}
    yield BASE | {"labels": {"EN": "One", "en": "Two"}}
    for key in [
        "system.built_in",
        "User.synthetic",
        "user.Synthetic",
        "user.synthetic-test",
        "user.x",
        "user." + "n" * 64,
    ]:
        yield BASE | {"key": key}
    for modes in [[], ["query", "create", "create"], ["execute"], "delete", None]:
        yield BASE | {"allowed_operations": modes}
    for value in [True, False, "YES", "false", 1, 0.0, " true ", "unsupported", None]:
        for topology in ["point", "open_interval", "bounded_interval", "flexible", "unknown"]:
            yield BASE | {"derived_duration": value, "topology": topology}
    yield BASE | {"privacy": "sensitive"}
    yield BASE | {"privacy": "public"}
    yield BASE | {"schema": BASE["schema"], "payload_schema": BASE["schema"]}
    yield {key: value for key, value in BASE.items() if key != "schema"} | {
        "payload_schema": BASE["schema"]
    }
    for semantics, unit, field_type in [
        ("event_count", "count", "integer"),
        ("event_count", "count", "number"),
        ("event_count", "steps", "integer"),
        ("event_count", "distance", "integer"),
        ("gauge", "m/s", "integer"),
        ("interval_total", "steps", "integer"),
        ("event_total", None, "integer"),
        ("cumulative_counter", "steps", "integer"),
    ]:
        for topology in ["point", "bounded_interval"]:
            variant = copy.deepcopy(BASE)
            variant["topology"] = topology
            variant["schema"]["properties"]["sample"]["type"] = field_type
            variant["fields"]["sample"].update(metric_semantics=semantics, unit=unit)
            yield variant
    for identity in ["$", "ID", "user.synthetic_counter.elapsed_minutes", "j" * 128, "j" * 129]:
        variant = copy.deepcopy(BASE)
        variant.update(derived_duration=True, topology="flexible")
        variant["fields"]["sample"]["id"] = identity
        yield variant
    for reserved in ["type", "sample_copy"]:
        variant = copy.deepcopy(BASE)
        variant["schema"]["properties"][reserved] = {"type": "string", "maxLength": 10}
        variant["fields"][reserved] = variant["fields"]["sample"]
        yield variant
    variant = copy.deepcopy(BASE)
    variant["schema"]["properties"]["sample"] = {"$ref": "#/$defs/EventCount"}
    variant["schema"]["$defs"] = {"EventCount": {"oneOf": [{"enum": [1, 3, 5]}, {"type": "null"}]}}
    variant["fields"]["sample"]["metric_semantics"] = "event_count"
    yield variant

    for literals in [
        {"const": 2**64},
        {"const": -(2**63) - 1},
        {"enum": [None, 2**169, -(2**170)]},
        {"enum": [None, 2**169, 1.0]},
        {"const": float(2**64)},
        {"const": True},
        {"anyOf": [{"const": 2**169}, {"type": "null"}]},
    ]:
        variant = copy.deepcopy(BASE)
        variant["schema"]["properties"]["sample"] = literals
        variant["fields"]["sample"]["metric_semantics"] = "event_count"
        yield variant


def expected(document):
    try:
        spec = DefinitionSpec.model_validate(document)
    except (TypeError, ValueError):
        return None
    contract = spec.model_dump(mode="json", by_alias=True)
    contract["allowed_operations"] = sorted(contract["allowed_operations"])
    return [spec.key, contract_hash(spec), contract]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    examples = [[document, expected(document)] for document in examples()]
    args.output.write_text(
        json.dumps(examples, ensure_ascii=True, allow_nan=False), encoding="utf-8"
    )
