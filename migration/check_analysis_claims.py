"""Capture verified analysis citations from pinned Python without provider calls."""

import argparse
import json
from pathlib import Path

from garmin_ai.claims import NumericClaim, TimestampClaim, verified_numbers, verified_timestamps

START = "2026-10-25T00:30:19+00:00"
END = "2026-10-25T01:30:41+00:00"
ZONE = "Europe/Bratislava"


def item(result, *, tool="analysis_sleep", identity=1):
    return {"id": identity, "tool": tool, "result": result}


def claim(value, path=None, *, identity=1):
    return {"evidence_id": identity, "path": path or ["value"], "value": value}


def capture(output):
    numeric = [
        ("exact_integer", [item({"value": 78})], [claim(78)], [1]),
        ("int_matches_float", [item({"value": 78.0})], [claim(78)], [1]),
        ("float_matches_int", [item({"value": 78})], [claim(78.0)], [1]),
        ("signed_zero", [item({"value": -0.0})], [claim(0)], [1]),
        ("small_float", [item({"value": 1e-5})], [claim(1e-5)], [1]),
        ("large_float", [item({"value": 1e16})], [claim(1e16)], [1]),
        ("fraction", [item({"value": -1.20005})], [claim(-1.20005)], [1]),
        ("huge_integer", [item({"value": 10**400})], [claim(10**400)], [1]),
        ("beyond_safe_integer", [item({"value": 2**53 + 1})], [claim(float(2**53))], [1]),
        ("wrong_value", [item({"value": 87})], [claim(78)], [1]),
        ("boolean_evidence", [item({"value": True})], [claim(1)], [1]),
        ("boolean_claim", [item({"value": 1})], [claim(True)], [1]),
        ("numeric_string", [item({"value": "1"})], [claim(1)], [1]),
        ("escaped_key", [item({"a/b~c": 1})], [claim(1, ["a/b~c"])], [1]),
        ("list_index", [item({"rows": [{"0": 4}]})], [claim(4, ["rows", 0, "0"])], [1]),
        ("wrong_index_type", [item({"rows": [4]})], [claim(4, ["rows", "0"])], [1]),
        ("wrong_key_type", [item({"0": 4})], [claim(4, [0])], [1]),
        ("oversized_index", [item({"rows": [4]})], [claim(4, ["rows", 10001])], [1]),
        ("marked_error", [item({"value": 1, "error": None})], [claim(1)], [1]),
        ("excluding_evidence", [item({"value": 1})], [claim(1)], [2]),
        ("missing_citation", [item({"value": 1})], [claim(1, identity=2)], [1, 2]),
        (
            "different_queries",
            [item({"value": 1}), item({"value": 2}, identity=2)],
            [claim(1), claim(2, identity=2)],
            [1, 2],
        ),
    ]
    sleep = item({"rows": [{"main_interval": {"start": START, "end": END}}]})
    start_path = ["rows", 0, "main_interval", "start"]
    end_path = ["rows", 0, "main_interval", "end"]
    timestamps = [
        ("first_fold", [sleep], [claim(START, start_path)], [1], "ru"),
        ("second_fold", [sleep], [claim(END, end_path)], [1], "ru"),
        ("english", [sleep], [claim(START, start_path)], [1], "en-US"),
        ("slovak", [sleep], [claim(END, end_path)], [1], "sk"),
        ("fake_timestamp_at_real_path", [sleep], [claim(END, start_path)], [1], "ru"),
        ("unselected_timestamp", [sleep], [claim(START, start_path)], [2], "en"),
    ]
    for timestamp in [
        "2026-10-15T22:17:00Z",
        "2026-10-15T22:17:00.0005+00:00",
        "2026-10-15T22:17:00-05:00",
        "2026-10-15T22:17:00",
        "2026-10-15",
        "2026-10-15T22:17:60+00:00",
        "9999-12-31T23:30:00+00:00",
        "1794916800",
    ]:
        timestamps.append(
            (timestamp, [item({"value": timestamp}, tool="fixture")], [claim(timestamp)], [1], "ru")
        )

    recorded = []
    for name, evidence, claims, selected in numeric:
        try:
            result = verified_numbers(
                [NumericClaim.model_validate(c) for c in claims], evidence, set(selected)
            )
        except ValueError:
            result = None
        recorded.append(
            {
                "name": name,
                "evidence": evidence,
                "claims": claims,
                "selected": selected,
                "result": result,
            }
        )
    for name, evidence, claims, selected, locale in timestamps:
        try:
            result = verified_timestamps(
                [TimestampClaim.model_validate(c) for c in claims],
                evidence,
                set(selected),
                ZONE,
                locale=locale,
            )
        except ValueError:
            result = None
        recorded.append(
            {
                "name": name,
                "evidence": evidence,
                "claims": claims,
                "selected": selected,
                "locale": locale,
                "timezone": ZONE,
                "result": result,
            }
        )
    output.write_text(json.dumps(recorded, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)


if __name__ == "__main__":
    main()
