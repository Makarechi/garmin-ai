"""Freeze typed generic-analysis normalization and hashes using synthetic arguments."""

import argparse
import json
import random
from datetime import UTC, datetime
from pathlib import Path

from garmin_ai.generic_analytics import AnalysisSpec, spec_hash

BASE = {
    "start": "2026-10-25T09:00:00Z",
    "end": "2026-10-26T09:00:00Z",
    "knowledge_cutoff": "2026-11-01T00:00:00Z",
}
OPERATIONS = [
    "query_entries",
    "query_observations",
    "aggregate_metric",
    "compare_periods",
    "query_completeness",
    "threshold_segments",
    "threshold_by_local_hour",
]


def arguments(operation):
    result = {**BASE, "operation": operation}
    if operation == "query_entries":
        result["definition_key"] = "user.synthetic"
    else:
        result["metric_key"] = "system.synthetic"
    if operation == "compare_periods":
        result.update(
            comparison_start="2026-10-01T09:00:00Z", comparison_end="2026-10-02T09:00:00Z"
        )
    if operation.startswith("threshold_"):
        result.update(threshold=51, threshold_operator="gte")
    if operation == "threshold_by_local_hour":
        result["timezone"] = "Europe/Bratislava"
    return result


def cases():
    for operation in OPERATIONS:
        base = arguments(operation)
        yield base
        for field, values in {
            "operation": [None, "", True, "missing"],
            "metric_key": [
                None,
                "",
                123,
                "system.a",
                "user.a-b.c_d",
                "system.A",
                "system.a\n",
                "system." + "a" * 128,
            ],
            "definition_key": [None, "", 123, "system.a", "user.a-b.c_d"],
            "method": [None, "mean", "counts", "_", False, "", "mean max", "Mean", "x" * 33],
            "metric_version": [
                None,
                True,
                False,
                0,
                1,
                2,
                2147483648,
                10**100,
                1.0,
                1.5,
                "2.00",
                "1_0",
                "1e2",
            ],
            "limit": [None, True, False, 0, 1, 1000, 1001, 10**100, 1.0, 1.5, "2.00", "1_0", "1e2"],
            "time_relation": [None, "starts_within", "overlap", "missing"],
            "source": [
                None,
                "event",
                "measurement:synthetic",
                "measurement:",
                "observation:[null,null]",
                "observation: [ 'a', null ]",
                'observation:["Café 🧪",null]',
                'observation:[null,"device"]',
                "observation:[null]",
                "observation:[1,null]",
                "observation:bad",
                "x" * 201,
            ],
            "threshold": [None, 0, 51.0, -0.0, 1e100, "51", True],
            "threshold_operator": [None, "gt", "gte", "lt", "lte", "bad", 1],
            "timezone": [
                None,
                "UTC",
                "US/Eastern",
                "Europe/Bratislava",
                "Pacific/Kiritimati",
                "",
                "missing/zone",
                "x" * 101,
            ],
            "start": [
                None,
                False,
                0,
                1792918800,
                1792918800000,
                1792918800.123456,
                "2026-10-25T09:00:00",
                "2026-10-25T11:00:00+02:00",
                "2026-10-25T09:00:00.123456789Z",
                "0001-01-01T00:00:00+01:00",
            ],
            "end": [
                None,
                "2026-10-25T09:00:00Z",
                "2026-10-24T09:00:00Z",
                "2027-10-26T09:00:00Z",
                "2027-10-27T09:00:00Z",
            ],
            "knowledge_cutoff": [
                None,
                False,
                0,
                1792918800.123456,
                "2026-10-25T09:00:00.100000-04:00",
                "0001-01-01T00:00:00+01:00",
                "9999-12-31T23:59:59-01:00",
            ],
            "comparison_start": [None, "2026-10-01T00:00:00Z", False],
            "comparison_end": [None, "2026-10-01T00:00:00Z", "2028-10-01T00:00:00Z"],
            "unknown": [True],
        }.items():
            for value in values:
                yield {**base, field: value}
        for field in (
            "operation",
            "start",
            "end",
            "knowledge_cutoff",
            "metric_key",
            "definition_key",
        ):
            yield {key: value for key, value in base.items() if key != field}
    generator = random.Random(4417)
    for _ in range(700):
        base = arguments(generator.choice(OPERATIONS))
        base["start"] = generator.choice(
            [
                "2026-10-25T09:00:00Z",
                "2026-10-25T12:00:00+03:00",
                "2026-10-25T04:00:00.000001-05:00",
            ]
        )
        base["metric_version"] = generator.choice([None, 1, "2.000", True, 10**30])
        base["limit"] = generator.choice([1, 17, "100.0", 1000, 1001])
        base["source"] = generator.choice(
            [None, "event", "measurement:synthetic", 'observation:["Кофе 😀",null]']
        )
        yield base

    for operation in OPERATIONS:
        for field, value in [
            ("limit", 0),
            ("limit", 1),
            ("source", "bad"),
            ("source", "x" * 201),
            ("threshold", True),
            ("metric_version", 0),
            ("comparison_start", False),
            ("method", "mean"),
        ]:
            yield {**arguments(operation), "start": "0001-01-01T00:00:00+01:00", field: value}

    for operation in OPERATIONS:
        for unit in [
            0,
            1,
            8,
            9,
            10,
            12,
            13,
            31,
            32,
            34,
            47,
            92,
            126,
            127,
            128,
            0xD7FF,
            0xD800,
            0xDBFF,
            0xDC00,
            0xDFFF,
            0xE000,
            0xFFFF,
        ]:
            yield {**arguments(operation), "source": f'observation:["\\u{unit:04x}",null]'}
        for value in [
            r'observation:["\ud83d\ude00",null]',
            r'observation:["a\/b",null]',
            r'observation:["\\ud800",null]',
            r'observation:["\uD800",null]',
            r'observation:["\uXYZZ",null]',
            r'observation:["\q",null]',
            r'observation:["\u000",null]',
            'observation:["",null]',
        ]:
            yield {**arguments(operation), "source": value}


def microseconds(value):
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86400000000 + delta.seconds * 1000000 + delta.microseconds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    output = parser.parse_args().output
    unique = {json.dumps(value, sort_keys=True, ensure_ascii=True): value for value in cases()}
    results = []
    for value in unique.values():
        try:
            parsed = AnalysisSpec.model_validate(value)
            results.append(
                {
                    "input": value,
                    "normalized": parsed.model_dump(mode="json"),
                    "hash": spec_hash(parsed),
                    "instants": {
                        "start": microseconds(parsed.start),
                        "end": microseconds(parsed.end),
                        "cutoff": microseconds(parsed.knowledge_cutoff),
                        "comparison": [
                            microseconds(parsed.comparison_start),
                            microseconds(parsed.comparison_end),
                        ]
                        if parsed.comparison_start is not None and parsed.comparison_end is not None
                        else None,
                    },
                }
            )
        except (ValueError, OverflowError, TypeError) as error:
            results.append({"input": value, "error": type(error).__name__})
    encoded = [json.dumps(case, ensure_ascii=True, separators=(",", ":")) for case in results]
    output.write_text("[\n" + ",\n".join(encoded) + "\n]\n", encoding="utf-8")
    print(f"Captured {len(results)} generic-analysis specifications and exact hashes")


if __name__ == "__main__":
    main()
