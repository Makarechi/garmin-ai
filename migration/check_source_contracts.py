"""Generate synthetic page-admission and record-digest cases with pinned Python."""

import argparse
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from garmin_ai.source_contracts import SourceCapabilities, SourcePage, record_overlaps_window

BASE_RECORD = {
    "source_record_id": "пример😀",
    "observed_at": "2025-03-30T14:12:30.000010+02:00",
    "effective_at": "2025-03-30T09:00:00+02:00",
    "source_timezone": "Europe/Bratislava",
    "source_reference": "integration:synthetic/sample",
    "payload": {"scope": {"i": 2**115 + 1, "f": -0.0, "exp": 0.0000125}, "text": "Crème"},
}
BASE_PAGE = {
    "instance_id": "synthetic:source",
    "page_kind": "partial",
    "fetched_at": "2025-03-30T14:00:00Z",
    "next_cursor": "cursor😀",
    "records": [BASE_RECORD],
}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def json_string(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def corpus():
    cases = []

    def add(name, page, caps=None, request=None, wire=None):
        cases.append(
            {
                "name": name,
                "wire": json_string(page) if wire is None else wire,
                "caps": caps,
                "request": request,
            }
        )

    def record_case(key, value, *, name=None, caps=None, request=None):
        page = copy.deepcopy(BASE_PAGE)
        page["records"][0][key] = value
        add(name or key, page, caps=caps, request=request)

    add("baseline", BASE_PAGE)
    for name, value in [
        ("earlier", "2025-03-30T01:02:03-07:00"),
        ("lowercase", "2025-03-30t01:02:03z"),
        ("numeric", 1743292800),
        ("milliseconds", "1743292800999"),
        ("fraction", 1743292800.120001),
        ("naive", "2025-03-30T01:02:03"),
        ("date_only", "2025-03-30"),
        ("invalid_year", "0000-01-01T02:03:04Z"),
        ("negative_zero_offset", "2025-03-30 01:02:03-00:00"),
        ("nanoseconds", "2025-03-30T01:02:03.303030999Z"),
        ("compact_offset", "2025-03-30T01:02:03+0130"),
    ]:
        record_case("observed_at", value, name=name)
    for zone in ["UTC", "utc", "US/Eastern", "Etc/GMT+8", "Europe/Bratislava", "invalid/zone"]:
        record_case("source_timezone", zone, name=f"zone-{zone}")
    for field, cap in [("source_record_id", 200), ("source_reference", 500)]:
        for length in [0, cap, cap + 1]:
            record_case(field, "я" * length, name=f"{field}-{length}")
    for length in [16_383, 16_384, 16_385]:
        record_case("payload", {"blob": "x" * (length - 12)}, name=f"byte-limit-{length}")
    for length in [255, 256, 257]:
        record_case("payload", {"array": [2] * length}, name=f"array-{length}")
        record_case("payload", {str(index): True for index in range(length)}, name=f"map-{length}")
    for depth in [7, 8, 9]:
        value = 1
        for _ in range(depth):
            value = {"item": value}
        record_case("payload", value, name=f"nesting-{depth}")
    record_case("payload", {"bad": float("nan")}, name="non-finite")
    record_case("payload", None, name="null-payload")
    record_case("payload", ["one"], name="array-payload")
    record_case("unknown", "value", name="unknown-field")
    record_case("effective_end", "2025-03-30T07:00:00Z", name="equal-end-utc")
    record_case("effective_end", "2025-03-30T07:00:01Z", name="after-start-utc")
    delete = {**BASE_RECORD, "payload": {}, "operation": "delete"}
    add("delete", {**BASE_PAGE, "records": [delete]})
    record_case("operation", "delete", name="deletion-with-payload")
    add("duplicate-identity", {**BASE_PAGE, "records": [BASE_RECORD, BASE_RECORD]})
    add("invalid-page-kind", {**BASE_PAGE, "page_kind": "full"})
    add("snapshot-cursor", {**BASE_PAGE, "page_kind": "complete_interval_snapshot"})
    add("overlong-cursor", {**BASE_PAGE, "next_cursor": "a" * 1001})
    add(
        "page-record-count",
        {
            **BASE_PAGE,
            "records": [
                {
                    "source_record_id": str(i),
                    **{key: val for key, val in BASE_RECORD.items() if key != "source_record_id"},
                }
                for i in range(1001)
            ],
        },
    )
    add(
        "last-duplicate-replaces-prior",
        BASE_PAGE,
        wire=json_string(BASE_PAGE).replace('"text":"Crème"', '"text":NaN,"text":"Crème"'),
    )

    window = {
        "instance_id": BASE_PAGE["instance_id"],
        "start": "2025-03-30T07:00:00Z",
        "end": "2025-03-30T08:00:00Z",
        "cursor": None,
        "previous": [],
    }
    caps = {"cursor": True}
    add("instant-includes-start", BASE_PAGE, caps, window)
    add("wrong-instance", BASE_PAGE, caps, {**window, "instance_id": "other"})
    add("missing-cursor-capability", BASE_PAGE, {"cursor": False}, window)
    add("missing-observation-capability", BASE_PAGE, {"observations": False}, window)
    add("cyclic-cursor", BASE_PAGE, caps, {**window, "cursor": BASE_PAGE["next_cursor"]})
    add(
        "historical-cursor",
        BASE_PAGE,
        caps,
        {**window, "cursor": "recent", "previous": [BASE_PAGE["next_cursor"]]},
    )
    add(
        "new-window-ignores-older-cursors",
        BASE_PAGE,
        caps,
        {**window, "previous": [BASE_PAGE["next_cursor"]]},
    )
    add(
        "cursor-capacity",
        BASE_PAGE,
        caps,
        {**window, "cursor": "recent", "previous": [str(i) for i in range(1000)]},
    )
    add(
        "snapshot-after-cursor",
        {**BASE_PAGE, "page_kind": "complete_interval_snapshot", "next_cursor": None},
        caps,
        {**window, "cursor": "recent"},
    )
    add("interval-needs-end", BASE_PAGE, {**caps, "time_semantics": "interval"}, window)
    add(
        "instant-excludes-end",
        BASE_PAGE,
        caps,
        {**window, "start": "2025-03-30T06:00:00Z", "end": "2025-03-30T07:00:00Z"},
    )
    add(
        "calendar-day-crosses-spring-transition",
        BASE_PAGE,
        {**caps, "time_semantics": "calendar_day"},
        {**window, "start": "2025-03-30T21:59:00Z", "end": "2025-03-30T22:05:00Z"},
    )
    add(
        "calendar-day-end-exclusive",
        BASE_PAGE,
        {**caps, "time_semantics": "calendar_day"},
        {**window, "start": "2025-03-30T22:00:00Z", "end": "2025-03-30T23:00:00Z"},
    )
    add(
        "explicit-deletion-capability",
        {**BASE_PAGE, "records": [delete]},
        {**caps, "deletions": "yes"},
        window,
    )
    add("undeclared-deletion", {**BASE_PAGE, "records": [delete]}, caps, window)
    for size in [1, 1000, "1.0", "+12", "1_00", "1e1", True, False, 1.1, -1, 1001, None, "12."]:
        add(f"capability-page-size-{size!r}", BASE_PAGE, {**caps, "max_page_size": size}, window)
    return cases


def observe(case):
    try:
        page = SourcePage.model_validate_json(case["wire"])
    except ValidationError:
        return {"valid": False}
    canonical = [json_string(item.model_dump(mode="json")) for item in page.records]
    result = {"valid": True, "records": canonical}
    if (request := case["request"]) is not None:
        try:
            caps = SourceCapabilities.model_validate(case["caps"])
        except ValidationError:
            result["request_valid"] = False
            return result
        start, end = (datetime.fromisoformat(request[name]) for name in ("start", "end"))
        cursor = request["cursor"]
        next_digest = digest(page.next_cursor) if page.next_cursor is not None else None
        previous = {digest(item) for item in request["previous"]} if cursor is not None else set()
        if cursor is not None:
            previous.add(digest(cursor))
        result["request_valid"] = (
            page.instance_id == request["instance_id"]
            and caps.observations
            and len(page.records) <= min(100, caps.max_page_size)
            and (page.page_kind != "complete_interval_snapshot" or cursor is None)
            and (
                next_digest is None
                or (caps.cursor and next_digest not in previous and len(previous) < 1000)
            )
            and all(
                (caps.deletions or record.operation != "delete")
                and record_overlaps_window(record, caps.time_semantics, start, end)
                for record in page.records
            )
        )
        if result["request_valid"]:
            result["next"] = next_digest
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    cases = corpus()
    args.output.write_text(
        json_string([{**case, "expected": observe(case)} for case in cases]),
        encoding="utf-8",
    )
    print(f"Generated {len(cases)} source contract cases")
