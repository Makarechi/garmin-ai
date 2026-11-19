"""Capture synthetic request normalization and tracker relevance contracts."""

import argparse
import itertools
import json
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from garmin_ai.natural_language import NaturalLanguageRequest, _score


def capture():
    valid = {"text": "Synthetic Focus", "operation_id": "synthetic-operation"}
    requests = [None, [], {}, valid, {**valid, "extra": 1}]
    values = [None, True, False, 0, 1, 1.0, [], {}, "", " ", "Focus", '🧪\n"\\']
    for key in ["text", "operation_id", "selected_event_id", "selected_definition_version_id"]:
        requests.extend({**valid, key: value} for value in values)
    for key in ["text", "operation_id"]:
        limit = 16000 if key == "text" else 160
        requests.extend(
            {**valid, key: value * size}
            for value, size in itertools.product(["x", "🧪", "\x00"], [limit - 1, limit, limit + 1])
        )
    for identity in [
        "00000000-0000-0000-0000-000000000042",
        "00000000000000000000000000000042",
        "{00000000-0000-0000-0000-000000000042}",
        "urn:uuid:00000000-0000-0000-0000-000000000042",
        "URN:UUID:00000000-0000-0000-0000-000000000042",
        "00000000-00000000-00000000-00000042",
    ]:
        requests.extend(
            {**valid, key: identity}
            for key in ["selected_event_id", "selected_definition_version_id"]
        )
    normalized = []
    for raw in requests:
        try:
            request = NaturalLanguageRequest.model_validate(raw)
        except ValidationError:
            normalized.append({"input": raw, "valid": False})
            continue
        canonical = request.model_dump_json(exclude_none=False)
        hashes = [sha256(canonical.encode()).hexdigest()]
        if request.selected_definition_version_id is None:
            hashes.append(
                sha256(
                    request.model_dump_json(
                        exclude={"selected_definition_version_id"}, exclude_none=False
                    ).encode()
                ).hexdigest()
            )
        normalized.append(
            {"input": raw, "valid": True, "document": json.loads(canonical), "hashes": hashes}
        )
    terms = [
        "Focus",
        "Daily focus",
        "focus_thing",
        "FOCUS",
        "Straße",
        "STRASSE",
        "\ua7ce",
        "\ua7cf",
        "BP",
        "кофе",
        "Фокус",
        "фокусный",
        "Synthetic",
        "synthesis",
        "synthetic-long",
        "e\u0301",
        "é",
        "नमस्ते",
    ]
    rankings = []
    for text, values in itertools.product(
        terms + ["my Focus coffee", "Фокус дня", "", "123 1234 12345 123456"],
        [[term] for term in terms] + [terms, [], ["Focus", "coffee", "focus"]],
    ):
        rankings.append([text, values, _score(text, values)])
    return {"requests": normalized, "scores": rankings}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(capture(), ensure_ascii=False, separators=(",", ":")) + "\n")
