"""Capture pure tracker-extraction evidence from the pinned source regressions."""

import argparse
import functools
import json
import sys
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

import garmin_ai.natural_language as source


def document(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, timedelta):
        return {"seconds": value.total_seconds()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, SimpleNamespace):
        return document(vars(value))
    if isinstance(value, dict):
        return {str(key): document(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(document(item) for item in value)
    if isinstance(value, (tuple, list)):
        return [document(item) for item in value]
    return value


class PureTests:
    def pytest_collection_modifyitems(self, config, items):
        selected, skipped = [], []
        for item in items:
            (skipped if {"db", "db_engine"}.intersection(item.fixturenames) else selected).append(
                item
            )
        items[:] = selected
        config.hook.pytest_deselected(items=skipped)


def capture(include_database=False):
    rows = []
    originals = {}
    names = [
        "_verify_evidence",
        "_temporal_number_spans",
        "_complete_number_in_source",
        "_has_uncertain_fact_context",
        "_has_contextual_negation",
        "_value_is_evidenced",
        "_categorical_value_is_evidenced",
        "_unit_spans",
        "_explicit_offset_matches",
        "_approximate_temporal_span",
        "_clock_tokens",
        "_datetime_evidence",
        "_dotted_quantity_spans",
        "_validated_submission",
    ]
    for name in names:
        original = getattr(source, name)
        originals[name] = original

        def wrap(original, name):
            @functools.wraps(original)
            def call(*args, **kwargs):
                row = {"function": name, "args": document(args), "kwargs": document(kwargs)}
                try:
                    result = original(*args, **kwargs)
                except (ValueError, TypeError, IndexError) as error:
                    row["error"] = type(error).__name__
                    row["message"] = str(error)
                    rows.append(row)
                    raise
                row["result"] = document(result)
                rows.append(row)
                return result

            return call

        setattr(source, name, wrap(original, name))
    try:
        plugins = [] if include_database else [PureTests()]
        if include_database:
            from capture_reference import isolated_engine

            engine = isolated_engine()
            engine.dispose()
        status = pytest.main(["-q", "tests/test_natural_language.py"], plugins=plugins)
        assert status == 0, f"Frozen pure regressions failed with {status}"
    finally:
        for name, original in originals.items():
            setattr(source, name, original)
    unique = {json.dumps(row, ensure_ascii=False, sort_keys=True): row for row in rows}
    return {"evidence": list(unique.values())}


def capture_calendar_bounds():
    rows = []
    current = datetime(2026, 9, 20, 20, tzinfo=UTC)
    for year, timezone in [
        (9999, "UTC"),
        (9999, "Europe/Bratislava"),
        (9999, "Etc/GMT-14"),
        (9999, "America/New_York"),
        (1, "UTC"),
    ]:
        start = datetime(
            year,
            12 if year == 9999 else 1,
            31 if year == 9999 else 1,
            12,
            30,
            tzinfo=ZoneInfo(timezone),
        ).astimezone(UTC)
        end = start + timedelta(minutes=15)
        day = "9999-12-31" if year == 9999 else "0001-01-01"
        text = f"{day} from 12:30 to 12:45"
        args = [start, text, timezone, current]
        result = source._datetime_evidence(
            *args, interval_hyphens=True, interval_clocks=((12, 30), (12, 45))
        )
        assert result.status == "supported", (timezone, year, result)
        rows.append(
            {
                "function": "_datetime_evidence",
                "args": document(args),
                "kwargs": {"interval_hyphens": True, "interval_clocks": [[12, 30], [12, 45]]},
                "result": document(result),
            }
        )
        evidence = source.SourceEvidence(start=0, end=len(text), quote=text)
        extraction = source.TrackerExtraction(
            schema_version="tracker.nl.v1",
            intent="create_entry",
            definition_version_id=UUID(int=42),
            start=start,
            end=end,
            start_evidence=evidence,
            end_evidence=evidence,
            confidence=1,
        )
        form = SimpleNamespace(
            topology="bounded_interval",
            initial_start=None,
            initial_end=None,
            initial_timezone=None,
            initial_values={},
            initial_units={},
            id="create:00000000-0000-0000-0000-000000000042",
            schema_hash="0" * 64,
        )
        args = [text, extraction, {"fields": []}, form, timezone, current]
        result = source._validated_submission(*args)
        rows.append(
            {
                "function": "_validated_submission",
                "args": document(args),
                "kwargs": {},
                "result": document(result),
            }
        )
    return {"evidence": rows}


def capture_unicode(output):
    text = f"// Frozen CPython {sys.version.split()[0]} Unicode {unicodedata.unidata_version.split('.')[0]} mark and digit categories.\n"
    for name, predicate in [
        ("MARK", lambda ch: unicodedata.category(ch).startswith("M")),
        ("DIGIT", str.isdigit),
    ]:
        ranges = []
        for point in range(0x110000):
            if predicate(chr(point)):
                if ranges and ranges[-1][1] + 1 == point:
                    ranges[-1][1] = point
                else:
                    ranges.append([point, point])
        text += f"pub(super) const {name}: &[(u32, u32)] = &[\n"
        text += "".join(f"    (0x{start:x}, 0x{end:x}),\n" for start, end in ranges)
        text += "];\n"
    output.write_text(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--include-database", action="store_true")
    parser.add_argument("--unicode-output", type=Path)
    parser.add_argument("--calendar-output", type=Path)
    args = parser.parse_args()
    result = capture(args.include_database)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    if args.calendar_output:
        args.calendar_output.write_text(
            json.dumps(capture_calendar_bounds(), ensure_ascii=False, separators=(",", ":")) + "\n"
        )
    if args.unicode_output:
        capture_unicode(args.unicode_output)
    print("Captured", len(result["evidence"]), "unique evidence calls")
