"""Pin typed model extraction normalization without calling a model provider."""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from garmin_ai.natural_language import TrackerExtraction


def capture():
    base = {"schema_version": "tracker.nl.v1", "intent": "none", "confidence": 0.95}
    values = [None, True, False, -1, 0, 1, 0.9, "0.9", "1.0", "NaN", "", " ", [], {}, "synthetic"]
    cases = [None, [], {}, base, {**base, "extra": True}]
    for name in [
        "schema_version",
        "intent",
        "confidence",
        "definition_version_id",
        "event_id",
        "start",
        "end",
        "start_evidence",
        "end_evidence",
        "fields",
        "tracker_draft",
        "clarification",
    ]:
        cases.extend({**base, name: value} for value in values)
    identity = "00000000-0000-0000-0000-000000000042"
    for name in ["definition_version_id", "event_id"]:
        for value in [
            identity,
            identity.replace("-", ""),
            "{" + identity + "}",
            "urn:uuid:" + identity,
            "URN:UUID:" + identity,
        ]:
            cases.append({**base, name: value})
    for name in ["start", "end"]:
        for value in [
            "2026-09-20T19:03:00Z",
            "2026-09-20T19:03:00.123456+02:00",
            "2026-09-20T19:03:00",
            "2026-09-20",
            0,
            1,
            1.5,
            1790000000000,
        ]:
            cases.append({**base, name: value})
    evidence = {"start": 0, "end": 3, "quote": "now"}
    entry = {
        **base,
        "intent": "create_entry",
        "definition_version_id": identity,
        "start": "2026-09-20T20:00:00Z",
        "start_evidence": evidence,
    }
    cases.extend(
        [
            entry,
            {
                **base,
                "intent": "update_entry",
                "definition_version_id": identity,
                "event_id": identity,
            },
        ]
    )
    for name in ["start", "end", "quote"]:
        for value in values + [16000, 16001, "🧪" * 240, "🧪" * 241]:
            cases.append({**entry, "start_evidence": {**evidence, name: value}})
    field = {"field_id": "user.focus.value", "value": 3, "evidence": evidence}
    cases.append({**entry, "fields": [field]})
    for name in ["field_id", "value", "evidence", "unit", "unit_evidence"]:
        for value in values:
            cases.append({**entry, "fields": [{**field, name: value}]})
    cases.extend({**entry, "fields": [field] * size} for size in [0, 1, 32, 33])
    draft = {
        "key": "focus",
        "name": "Synthetic Focus",
        "fields": [{"key": "value", "label": "Value", "kind": "text"}],
    }
    for intent in ["propose_tracker", "change_tracker"]:
        for version in [None, identity]:
            cases.append(
                {**base, "intent": intent, "tracker_draft": draft, "definition_version_id": version}
            )
    result = []
    for raw in cases:
        try:
            normalized = TrackerExtraction.model_validate(raw).model_dump(mode="json")
        except ValidationError:
            result.append({"input": raw, "valid": False})
        else:
            result.append({"input": raw, "valid": True, "document": normalized})
    for token in [
        "1.000000000000000000123",
        "1e309",
        "1e20",
        "1e-400",
        "-0.0",
        "12345678901234567890",
    ]:
        raw = {**entry, "fields": [{**field, "value": "SYNTHETIC_RAW_NUMBER"}]}
        raw_json = json.dumps(raw).replace('"SYNTHETIC_RAW_NUMBER"', token)
        try:
            normalized = TrackerExtraction.model_validate_json(raw_json).model_dump(mode="json")
        except ValidationError:
            result.append({"raw_input": raw_json, "valid": False})
        else:
            result.append({"raw_input": raw_json, "valid": True, "document": normalized})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = capture()
    args.output.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n")
    print("Captured", len(rows), "model extraction contracts")
