"""Emit synthetic event context reference cases from the pinned Python model."""

import itertools
import json
import random
from datetime import UTC, datetime

from pydantic import ValidationError

from garmin_ai.events import EventInput

START = "2026-10-25T02:30:00+02:00"
LATER = "2026-10-25T02:30:00+01:00"
NOTE = {"type": "note", "description": "Synthetic"}
SOURCE_VALUES = (
    "manual",
    "telegram_text",
    "telegram_button",
    "telegram_voice",
    "mcp",
    "inferred",
    "wearable",
)
STATUSES = ("confirmed", "inferred", "needs_confirmation")
PAYLOADS = (
    {"type": "medication"},
    {"type": "medication", "name": "Synthetic A", "dose": 2, "unit": "mg"},
    {"type": "wellbeing_observation", "energy": 4},
    {"type": "activity_effort", "activity_id": "42", "perceived_exertion": 3},
    {
        "type": "symptom_observation",
        "episode_id": "00112233-4455-6677-8899-aabbccddeeff",
        "severity": 4,
    },
    {"type": "headache_observation", "headache": "unknown", "migraine": "no"},
    {"type": "caffeine_absence", "description": "Synthetic"},
    {"type": "caffeine_log_complete", "description": "Synthetic"},
    {"type": "migraine"},
    NOTE,
)
TIMESTAMPS = (
    START,
    LATER,
    "2026-01-01T12:34:56.123456789+00:00",
    "2026-01-01t12:34:56z",
    "2026-01-01_12:34:56Z",
    "2026-01-01 12:34:56Z",
    "2026-01-01T12:34Z",
    "2026-01-01T12:34+0000",
    "2026-01-01T12:34:56+00",
    "2026-01-01T12:34:56−01:30",
    "2026-01-01T12:34:60Z",
    "2026-01-01T12:34:56,123Z",
    "2026-01-01T12:34:56+23:59",
    " 2026-01-01T12:34:56Z ",
    "1768471123123",
    1768471123.1234567,
    "1768471123.1234567",
    True,
    0,
    20000000000,
    20000000001,
    -20000000000,
    -20000000001,
    0.0000006,
    -0.0000006,
    0.0000005,
    -0.0000005,
    "2e9",
    "1.2e3",
    "0.",
    "-.5",
    ".5",
    "1_000",
    "2026-01-02",
    "2026-01-01T12:34:56",
    "2026-01-01T12:34.5Z",
    "2026-01-01T12:34:56+24:00",
    "2026-01-01T12:34:56-0000",
    1768471123123.5678,
    -1768471123123.5678,
    9e30,
    None,
)


def micros(value):
    if value is None:
        return None
    difference = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (difference.days * 86400 + difference.seconds) * 1_000_000 + difference.microseconds


def inputs():
    for payload, source, status, end, audited in itertools.product(
        PAYLOADS, SOURCE_VALUES, STATUSES, (None, START, LATER), (False, True)
    ):
        yield (
            {"start": START, "end": end, "payload": payload, "source": source, "status": status},
            audited,
        )
    for value in TIMESTAMPS:
        yield {"start": value, "payload": NOTE}, False
    rng = random.Random(20261117)
    for _ in range(256):
        number = rng.uniform(-2e9, 2e9) * rng.choice((0.000001, 1, 1000))
        yield {"start": number, "payload": NOTE}, False
        yield {"start": str(number), "payload": NOTE}, False
    for value in (
        "UTC",
        "Europe/Bratislava",
        "Etc/UTC",
        "US/Eastern",
        "Europe/Prague",
        "europe/bratislava",
        "utc",
        "Etc/Unknown",
        "../UTC",
        "",
        None,
    ):
        yield {"start": START, "payload": NOTE, "timezone": value}, False
    for value in (0, -1, 1, 1.1, 0.5, True, " 0.5 ", "NaN", False, None):
        yield {"start": START, "payload": NOTE, "confidence": value}, False
    yield {"start": LATER, "end": START, "payload": NOTE}, False
    yield {"start": START, "payload": NOTE, "synthetic_unknown": True}, False
    for value in ("😃" * 16000, "😃" * 16001, "", True, None):
        yield {"start": START, "payload": NOTE, "original_text": value}, False


def main():
    for data, audited in inputs():
        try:
            report = EventInput.model_validate(data, context={"restore_audited_snapshot": audited})
        except (ValidationError, ValueError):
            expected = None
        else:
            expected = report.model_dump(mode="json")
            expected.update(start=micros(report.start), end=micros(report.end))
        print(
            json.dumps(
                {
                    "input": json.dumps(data, ensure_ascii=False),
                    "audited": audited,
                    "expected": expected,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
