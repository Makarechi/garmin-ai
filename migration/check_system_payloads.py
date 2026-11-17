"""Emit synthetic system diary payloads with outcomes from the pinned Python reference."""

import json
import random

from pydantic import TypeAdapter, ValidationError

from garmin_ai.events import Payload

ADAPTER = TypeAdapter(Payload)
IDENTITY = "00112233-4455-6677-8899-aabbccddeeff"
CONTEXTS = (
    "alcohol",
    "meal",
    "hydration",
    "illness",
    "nap",
    "stressor",
    "travel",
    "mood",
    "note",
    "context",
    "caffeine_absence",
    "caffeine_log_complete",
)
VALUES = (
    None,
    True,
    False,
    0,
    -1,
    1,
    10,
    11,
    30,
    31,
    5000,
    5001,
    100000,
    100001,
    0.5,
    1.0,
    1.000001,
    "",
    " ",
    "\u001c",
    "😃",
    " 01 ",
    "+1",
    "1.0",
    "1_0",
    "1__0",
    "1_0.2",
    "1e0",
    "1.0_0",
    "1.00",
    "1.",
    "Infinity",
    "NaN",
    "yes",
    "true",
    "no",
    "unknown",
    [],
    {},
    [""],
    ["synthetic"] * 30,
    ["synthetic"] * 31,
    [1],
    "mg",
    "IU",
    "iu",
    IDENTITY,
    IDENTITY.upper(),
    IDENTITY.replace("-", ""),
    "{" + IDENTITY + "}",
    "urn:uuid:" + IDENTITY,
)


def sources():
    yield from ([], {}, {"type": "synthetic_unknown"}, {"type": []})
    for context in CONTEXTS:
        yield {"type": context, "description": "Synthetic context"}
    templates = [
        (
            {"type": "caffeine", "beverage": "Synthetic drink"},
            (
                "servings",
                "dose_basis",
                "dose_provenance",
                "dose_notes",
                "caffeine_mg_estimate",
                "beverage",
            ),
        ),
        ({"type": "migraine"}, ("severity", "aura", "notes", "symptoms")),
        (
            {"type": "symptom_observation", "episode_id": IDENTITY, "severity": 1},
            ("episode_id", "aura", "symptoms", "impact"),
        ),
        ({"type": "medication"}, ("name", "dose", "unit", "reason_event_id")),
        (
            {"type": "activity_effort", "activity_id": "synthetic", "perceived_exertion": 3},
            ("activity_id", "perceived_exertion", "notes"),
        ),
        (
            {"type": "meal", "description": "Synthetic meal"},
            ("description", "amount", "unit", "tags"),
        ),
        (
            {"type": "headache_observation", "headache": "unknown", "migraine": "no"},
            ("headache", "migraine"),
        ),
        ({"type": "wellbeing_observation", "energy": 1}, ("energy", "functional_impact", "notes")),
    ]
    for base, fields in templates:
        yield base
        yield {**base, "synthetic_extra": True}
        for field in fields:
            for value in VALUES:
                yield {**base, field: value}
    for length in (199, 200, 201, 1000, 1001, 3999, 4000, 4001):
        yield {"type": "medication", "name": "😃" * length}
        yield {"type": "wellbeing_observation", "notes": "x" * length}
        yield {"type": "symptom_observation", "episode_id": IDENTITY, "impact": "x" * length}
        yield {"type": "migraine", "notes": "\N{COMBINING ACUTE ACCENT}" * length}
    for value in range(8):
        yield {
            "type": "caffeine",
            "beverage": "Synthetic drink",
            "caffeine_mg_min": 2,
            "caffeine_mg_max": 5,
            "caffeine_mg_estimate": value,
        }
    generator = random.Random(20261117)
    for _ in range(100):
        base, fields = generator.choice(templates)
        yield {**base, **{field: generator.choice(VALUES) for field in generator.sample(fields, 2)}}
    yield '{"type":"wellbeing_observation","energy":-0}'
    yield '{"type":"wellbeing_observation","energy":0e0}'
    yield '{"type":"medication","dose":1.2,"dose":null}'
    yield {"type": "symptom_observation", "episode_id": IDENTITY}
    yield {"type": "wellbeing_observation"}


def main():
    for candidate in sources():
        raw = candidate if isinstance(candidate, str) else json.dumps(candidate, ensure_ascii=False)
        try:
            expected = ADAPTER.validate_json(raw).model_dump(mode="json")
        except ValidationError:
            expected = None
        print(json.dumps({"input": raw, "expected": expected}, ensure_ascii=False))


if __name__ == "__main__":
    main()
