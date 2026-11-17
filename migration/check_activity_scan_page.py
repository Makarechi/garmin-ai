"""Capture mutable activity-page transition decisions from pinned Python."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from garmin_ai import activity_sync

ZONE = "Europe/Bratislava"
NOW = datetime.fromisoformat("2026-10-24T23:30:00.123456+00:00")


def page(first, count, at="2026-10-24T23:10:00+00:00"):
    return [{"activityId": index, "startTimeGMT": at} for index in range(first, first + count)]


def cursor(**overrides):
    return {
        "account": "a" * 64,
        "generation": "synthetic",
        "scan_key": "activity_scan:synthetic",
        "anchor": [],
        "since": "2026-10-25",
        "offset": 0,
        "round": 0,
        "backfill": False,
    } | overrides


def examples():
    yield cursor(), page(1, 100)
    yield cursor(), page(1, 100, "2026-10-24T21:59:59+00:00")
    yield cursor(), page(1, 100, "2026-10-24T22:00:00+00:00")
    yield cursor(), page(2**53, 100)
    yield cursor(backfill=True), page(1, 12)
    yield cursor(), page(1, 0)
    yield cursor(), page(1, 99)
    yield cursor(), page(1, 101)
    anchor = [str(index) for index in range(81, 101)]
    yield cursor(anchor=anchor, offset=80), page(81, 100)
    yield cursor(anchor=anchor, offset=80), page(80, 100)
    yield cursor(anchor=anchor, offset=80), page(82, 100)
    for round_number in range(5):
        yield cursor(anchor=anchor, offset=800, round=round_number, backfill=True), page(81, 19)
        yield cursor(anchor=anchor, offset=800, round=round_number), page(81, 20)
    yield cursor(), page(1, 99) + [{"activityId": "synthetic-without-clock"}]
    yield cursor(), page(1, 99) + [{"activityId": "synthetic-bad-clock", "startTimeGMT": "?"}]
    yield cursor(), page(1, 99) + [{"startTimeGMT": "2026-10-25T05:30:00Z"}]
    yield cursor(since="2026-10-26"), page(1, 100, 1_792_891_200_000)


def expected(payload, values):
    row = SimpleNamespace(
        key=payload["scan_key"], value={**payload, "status": "running", "pages": 2}
    )
    session = SimpleNamespace(get=lambda *_args, **_kwargs: row)
    with patch.object(activity_sync, "queue_page", return_value="synthetic-job"):
        try:
            assert activity_sync.finish_page(session, payload, values, ZONE, NOW)
        except (TypeError, KeyError, ValueError, OverflowError):
            return {"error": True}
    state = row.value
    status = state["status"]
    if status == "running":
        return {key: state[key] for key in ("status", "offset", "round", "anchor")}
    return {key: state[key] for key in ("status", "next_scan_at")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    cases = [
        {"cursor": payload, "values": values, "expected": expected(payload, values)}
        for payload, values in examples()
    ]
    parser.parse_args().output.write_text(
        json.dumps({"cases": cases, "now": NOW.isoformat(), "timezone": ZONE}),
        encoding="utf-8",
    )
    print(f"Captured {len(cases)} mutable activity-page transitions")
