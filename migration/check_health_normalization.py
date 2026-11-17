"""Emit synthetic input and the pinned legacy health normalizer's output."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import garmin_ai.normalize as reference
from garmin_ai.metrics import CATALOG

DAY = "2026-10-25"
NIGHT_START = "2026-10-24T22:00:00.500000Z"
NIGHT_END = "2026-10-25T05:45:00+00:00"
SAMPLE_AT = 1792888200123


def cases():
    yield from (
        ("daily", DAY, None),
        ("daily", DAY, []),
        ("daily", DAY, {}),
        ("daily", DAY, {"totalSteps": 0, "restingHeartRate": False, "averageStressLevel": -1}),
        ("daily", DAY, {"moderateIntensityMinutes": 2, "vigorousIntensityMinutes": 4.5}),
        ("daily", "unknown-date", {"totalSteps": 6000}),
        ("training_status", DAY, {"unused": [1, 2]}),
        ("daily", DAY, ["invalid"]),
        ("hydration", DAY, {"valueInML": 0}),
        ("max_metrics", DAY, [{"generic": {"vo2MaxPreciseValue": 59.1}}, {"generic": {}}]),
        ("max_metrics", DAY, [{"generic": {"vo2MaxPreciseValue": 62.5}}]),
        ("sleep", DAY, {"dailySleepDTO": {"sleepTimeSeconds": 27_600, "deepSleepSeconds": 4_300}}),
        (
            "sleep",
            DAY,
            {
                "dailySleepDTO": {
                    "sleepScores": {"overall": {"value": 91}},
                    "sleepStartTimestampGMT": NIGHT_START,
                    "sleepEndTimestampGMT": NIGHT_END,
                }
            },
        ),
        (
            "sleep",
            DAY,
            {"dailySleepDTO": {"sleepStartTimestampGMT": NIGHT_START, "sleepEndTimestampGMT": 1}},
        ),
        (
            "hrv",
            DAY,
            {
                "hrvSummary": {
                    "lastNightAvg": 31,
                    "weeklyAvg": 38,
                    "status": "BALANCED",
                    "baseline": {"balancedLow": 27, "balancedUpper": 48},
                },
                "hrvReadings": [
                    {"readingTimeGMT": SAMPLE_AT, "hrvValue": 0},
                    {"readingTimeGMT": SAMPLE_AT + 300000, "hrvValue": 41.75},
                ],
            },
        ),
        (
            "readiness",
            DAY,
            [
                {"calendarDate": "2026-10-24", "score": 99},
                {"score": 55, "timestamp": "2026-10-25 01:30:00", "recoveryTime": 18},
                {
                    "score": 58,
                    "calendarDate": DAY,
                    "timestamp": "2026-10-25T02:30:00+01:00",
                    "recoveryTimeChangePhrase": "REACHED_ZERO",
                },
                {"score": 110, "timestamp": "2026-10-25T02:30:00+01:00", "recoveryTime": 19},
            ],
        ),
        (
            "body_battery",
            DAY,
            [{"charged": 70, "drained": 5}, {"date": DAY, "drained": 0}],
        ),
        ("steps", DAY, [{"startGMT": "2026-10-25 00:00:00", "steps": 0}]),
        (
            "spo2",
            DAY,
            {"spO2HourlyAverages": [], "spO2ValuesArray": [[SAMPLE_AT, 95]]},
        ),
    )
    for values in ([], [[SAMPLE_AT], [], False], [[SAMPLE_AT, -1], [SAMPLE_AT + 1, 100.0]]):
        for endpoint, key in [
            ("heart_rate", "heartRateValues"),
            ("stress", "stressValuesArray"),
            ("respiration", "respirationValuesArray"),
            ("spo2", "spO2ValuesArray"),
        ]:
            yield endpoint, DAY, {key: values}
    for index in [-5, -2, -1, 0, 1, 2, 3, "1", "invalid"]:
        yield (
            "stress",
            DAY,
            {
                "stressValuesArray": [[SAMPLE_AT, 0], [SAMPLE_AT + 1, 2]],
                "bodyBatteryValueDescriptorsDTOList": [
                    {
                        "bodyBatteryValueDescriptorKey": "other",
                        "bodyBatteryValueDescriptorIndex": 0,
                    },
                    {
                        "bodyBatteryValueDescriptorKey": "bodyBatteryLevel",
                        "bodyBatteryValueDescriptorIndex": index,
                    },
                ],
                "bodyBatteryValuesArray": [[SAMPLE_AT, 33, 48], [SAMPLE_AT + 1, 54, 49]],
            },
        )
    for stamp in [SAMPLE_AT, 1792888200.12345, NIGHT_START, NIGHT_END, None, "bad"]:
        for value in [-2, -0.0, 0, 1, 50, 99.5, 101, 300, 301, False, "72"]:
            yield (
                "heart_rate",
                DAY,
                {"heartRateValues": [[stamp, value]], "restingHeartRate": value},
            )


def expected(endpoint, key, payload):
    output = {"fields": {}, "samples": [], "observations": [], "sleep_interval": None}

    def instant(value):
        return value.isoformat() if value else None

    def sample(_session, stamp, metric, value, unit, _ref, _timezone, **_bounds):
        spec = CATALOG[metric]
        value = reference.numeric(value, minimum=spec.minimum, maximum=spec.maximum)
        if value is not None and stamp is not None:
            output["samples"].append([instant(reference.timestamp(stamp)), metric, unit, value])

    def observation(
        _session,
        metric,
        value,
        unit,
        _day,
        _ref,
        _zone,
        sequence=0,
        *,
        observed_at=None,
        effective_start=None,
        effective_end=None,
    ):
        if value is not None:
            output["observations"].append(
                [
                    metric,
                    unit,
                    value,
                    sequence,
                    instant(observed_at),
                    [instant(effective_start), instant(effective_end)] if effective_start else None,
                ]
            )

    def fields(_session, _day, values, _endpoint, _ref):
        output["fields"] = {key: value for key, value in values.items() if value is not None}

    def interval(_session, _model, values, _keys):
        output["sleep_interval"] = [instant(values["start"]), instant(values["end"])]

    try:
        with (
            patch.object(reference, "sample", side_effect=sample),
            patch.object(reference, "observe", side_effect=observation),
            patch.object(reference, "health_fields", side_effect=fields),
            patch.object(reference, "upsert", side_effect=interval),
        ):
            output["status"] = reference._normalize(
                SimpleNamespace(),
                endpoint,
                key,
                payload,
                "synthetic",
                "Europe/Bratislava",
            )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, OverflowError):
        return {"status": "error"}
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    corpus = [
        [endpoint, key, payload, expected(endpoint, key, payload)]
        for endpoint, key, payload in cases()
    ]
    args.output.write_text(json.dumps(corpus, ensure_ascii=True, allow_nan=False), encoding="utf-8")
