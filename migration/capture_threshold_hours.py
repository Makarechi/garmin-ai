"""Capture exact local-hour slices from frozen Python, including clock changes."""

import json
import random
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from garmin_ai.generic_analytics import _local_hour_slices


def capture():
    randomizer = random.Random(31847)
    zones = [
        "UTC",
        "America/New_York",
        "Europe/Bratislava",
        "Asia/Kathmandu",
        "Australia/Lord_Howe",
        "Pacific/Apia",
        "Africa/Monrovia",
        "Europe/Dublin",
        "Antarctica/Casey",
        "Pacific/Chatham",
        "Asia/Colombo",
        "America/St_Johns",
    ]
    starts = [
        datetime(2026, 3, 8, 4, tzinfo=UTC),
        datetime(2026, 11, 1, 4, tzinfo=UTC),
        datetime(2026, 3, 29, tzinfo=UTC),
        datetime(2026, 10, 25, tzinfo=UTC),
        datetime(2026, 4, 4, 12, tzinfo=UTC),
        datetime(2026, 10, 3, 12, tzinfo=UTC),
        datetime(2011, 12, 29, tzinfo=UTC),
        datetime(1972, 1, 6, tzinfo=UTC),
        datetime(1919, 3, 30, tzinfo=UTC),
        datetime(1941, 8, 31, tzinfo=UTC),
        datetime(1883, 11, 18, tzinfo=UTC),
        datetime(1901, 12, 13, tzinfo=UTC),
    ]
    windows = [(start, start + timedelta(days=2)) for start in starts]
    for _ in range(350):
        start = datetime(randomizer.randrange(1850, 2101), 1, 1, tzinfo=UTC) + timedelta(
            seconds=randomizer.randrange(365 * 86400), microseconds=randomizer.randrange(1_000_000)
        )
        windows.append(
            (
                start,
                start
                + timedelta(
                    seconds=randomizer.randrange(1, 4 * 86400),
                    microseconds=randomizer.randrange(1_000_000),
                ),
            )
        )
    windows += [
        (datetime(1, 1, 1, tzinfo=UTC), datetime(1, 1, 1, 1, tzinfo=UTC)),
        (datetime(9999, 12, 31, 22, tzinfo=UTC), datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)),
        (
            datetime(2026, 11, 1, 5, 59, 59, 999999, tzinfo=UTC),
            datetime(2026, 11, 1, 6, 0, 0, 1, tzinfo=UTC),
        ),
    ]
    cases = []
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for index, (start, end) in enumerate(windows):
        selected = (
            zones
            if index < len(starts) or index >= len(windows) - 3
            else [randomizer.choice(zones)]
        )
        for zone in selected:
            case = {
                "zone": zone,
                "start": (start - epoch) // timedelta(microseconds=1),
                "end": (end - epoch) // timedelta(microseconds=1),
            }
            try:
                case["slices"] = [
                    [hour, str(day), seconds]
                    for hour, day, seconds in _local_hour_slices(start, end, ZoneInfo(zone))
                ]
            except Exception as error:
                case["error"] = type(error).__name__
            cases.append(case)
    return cases


if __name__ == "__main__":
    sys.stdout.write(
        "[\n"
        + ",\n".join(
            json.dumps(case, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            for case in capture()
        )
        + "\n]\n"
    )
