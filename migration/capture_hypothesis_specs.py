"""Freeze hypothesis validation and normalization using synthetic protocols."""

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from garmin_ai.hypotheses import HypothesisSpec

BASE = {
    "id": "12345678-1234-5678-9234-567812345678",
    "question": "Synthetic prospective question",
    "discovery_start": "2026-09-01",
    "discovery_end": "2026-09-08",
    "validation_start": "2026-09-13",
    "validation_end": "2026-10-10",
    "expires": "2026-10-20",
    "timezone": "UTC",
    "direction": "lower",
}

CLOCKS = [
    "2026-03-29T00:59:59.999999+00:00",
    "2026-03-29T01:00:00+00:00",
    "2026-10-25T23:00:00+00:00",
    "2026-11-01T03:59:59.999999+00:00",
    "2026-11-01T04:00:00+00:00",
    "2026-11-01T06:00:00+00:00",
    "0001-01-01T00:30:00+00:00",
    "9999-12-31T23:30:00+00:00",
]


def cases():
    yield BASE
    for field in BASE:
        yield {key: value for key, value in BASE.items() if key != field}
    for field, values in {
        "id": [
            None,
            True,
            1,
            "",
            "synthetic",
            BASE["id"].upper(),
            BASE["id"].replace("-", ""),
            "urn:uuid:" + BASE["id"],
            "{" + BASE["id"] + "}",
            "00000000-0000-0000-0000-000000000000",
        ],
        "question": [
            None,
            True,
            1,
            "",
            " ",
            "\t\n",
            "\x1c",
            "  Synthetic  ",
            "😀" * 1000,
            "Я" * 1001,
        ],
        "timezone": [
            None,
            1,
            "",
            " ",
            "Invalid/Synthetic",
            "../UTC",
            "UTC",
            "Etc/UTC",
            "America/New_York",
            "Europe/Bratislava",
            "Asia/Kathmandu",
            "posix/UTC",
            "right/UTC",
        ],
        "outcome": [None, True, "", "sleep_score", "sleep_seconds", "steps"],
        "direction": [None, True, "", "lower", "higher", "LOWER"],
        "late_hours": [
            None,
            True,
            False,
            0,
            -1,
            0.001,
            1,
            6,
            24,
            24.00001,
            "6",
            " 6 ",
            "1_0",
            "1__0",
            "1_",
            "_1",
            "１",
            "١",
            "1e1",
            "NaN",
            "Infinity",
            {},
            [],
        ],
        "extra": [None, True, 1, "synthetic"],
    }.items():
        for value in values:
            yield {**BASE, field: value}
    dates = [
        None,
        True,
        False,
        0,
        1,
        1756684800,
        1756684800000,
        1756684800.1,
        "",
        "2026-09-01",
        "2026-9-1",
        "2026-02-29",
        "2024-02-29",
        "0000-01-01",
        "0001-01-01",
        "9999-12-31",
        "2026-09-01T00:00:00",
        "2026-09-01T00:00:00Z",
        "2026-09-01T00:00:00+05:45",
        "2026-09-01T00:00:00.0000001Z",
        "2026-09-01T00:00:00.000001Z",
        "1756684800",
        "1756684800000",
        "2026-09-01T01:00:00Z",
    ]
    for field in [
        "discovery_start",
        "discovery_end",
        "validation_start",
        "validation_end",
        "expires",
    ]:
        for value in dates:
            yield {**BASE, field: value}
    for discovery in [-1, 0, 1, 29, 30, 31]:
        for validation in [0, 26, 27, 28, 30, 31]:
            for expiry in [1, 2, 31, 32]:
                first = datetime(2026, 9, 1, tzinfo=UTC)
                end = first + timedelta(days=discovery)
                start_validation = end + timedelta(days=5)
                end_validation = start_validation + timedelta(days=validation)
                yield {
                    **BASE,
                    "discovery_end": end.date().isoformat(),
                    "validation_start": start_validation.date().isoformat(),
                    "validation_end": end_validation.date().isoformat(),
                    "expires": (end_validation + timedelta(days=expiry)).date().isoformat(),
                }
    randomizer = random.Random(47181)
    date_fields = [
        "discovery_start",
        "discovery_end",
        "validation_start",
        "validation_end",
        "expires",
    ]
    for mode in [
        "seconds",
        "milliseconds",
        "float",
        "numeric-string",
        "midnight",
        "offset-midnight",
    ]:
        value = dict(BASE)
        for field in date_fields:
            instant = datetime.fromisoformat(BASE[field]).replace(tzinfo=UTC)
            stamp = int(instant.timestamp())
            value[field] = {
                "seconds": stamp,
                "milliseconds": stamp * 1000,
                "float": float(stamp),
                "numeric-string": str(stamp),
                "midnight": BASE[field] + "T00:00:00Z",
                "offset-midnight": BASE[field] + "T00:00:00+05:45",
            }[mode]
        yield value
    for _ in range(100):
        hours = randomizer.uniform(-5, 30)
        yield {**BASE, "late_hours": hours, "direction": randomizer.choice(["lower", "higher"])}


def run(output):
    unique = {json.dumps(case, sort_keys=True, ensure_ascii=True): case for case in cases()}
    result = []
    for value in unique.values():
        try:
            spec = HypothesisSpec.model_validate(value)
            local_dates = []
            for clock in CLOCKS:
                try:
                    local_dates.append(
                        {
                            "clock": clock,
                            "date": str(
                                datetime.fromisoformat(clock)
                                .astimezone(ZoneInfo(spec.timezone))
                                .date()
                            ),
                        }
                    )
                except OverflowError:
                    local_dates.append({"clock": clock, "error": "OverflowError"})
            result.append(
                {
                    "input": value,
                    "normalized": spec.model_dump(mode="json"),
                    "registration_normalized": HypothesisSpec.model_validate(
                        spec.model_dump()
                    ).model_dump(mode="json"),
                    "local_dates": local_dates,
                }
            )
        except Exception as error:
            result.append({"input": value, "error": type(error).__name__})
    output.write_text(
        "[\n"
        + ",\n".join(json.dumps(case, ensure_ascii=True, separators=(",", ":")) for case in result)
        + "\n]\n",
        encoding="utf-8",
    )
    print(
        "Captured hypothesis specifications",
        len(result),
        "accepted",
        sum("normalized" in case for case in result),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
