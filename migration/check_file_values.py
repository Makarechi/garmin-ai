"""Produce synthetic file import row decisions using the pinned Python implementation."""

import argparse
import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from garmin_ai.file_source import FileMapping, _jsonb_string_issue, _read_rows, _time, _value

BASE = {
    "format": "json",
    "source_instance_id": "synthetic-source",
    "device_id": "synthetic-device",
    "definition_key": "user.synthetic",
    "row_id_column": "id",
    "start_column": "start",
    "timezone": "Europe/Bratislava",
    "field_columns": {"field": "value"},
    "decimal_separator": ".",
    "null_markers": ["", "unknown"],
}
DATE = "2026-03-29T03:10:00+02:00"
EPOCH = datetime.fromtimestamp(0, UTC)


def microseconds(value):
    if value is None:
        return None
    elapsed = value - EPOCH
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def process(data, mapping, field_kind):
    seen = set()
    outcomes = []
    for row in _read_rows(data, mapping.format):
        try:
            if issue := _jsonb_string_issue(row):
                raise ValueError(issue)
            identity = row.get(mapping.row_id_column)
            if not isinstance(identity, str) or not 1 <= len(identity) <= 200:
                raise ValueError("source_id_required")
            if identity in seen:
                raise ValueError("duplicate_source_id")
            seen.add(identity)
            start = _time(row.get(mapping.start_column), mapping.timezone)
            end = None
            if mapping.end_column:
                raw_end = row.get(mapping.end_column)
                if raw_end is None or raw_end == "":
                    raise ValueError("end_required")
                end = _time(raw_end, mapping.timezone)
                if end <= start:
                    raise ValueError("end_after_start_required")
            if "value" not in row:
                outcomes.append({"code": "missing_column", "field": "field"})
                continue
            value = _value(row["value"], SimpleNamespace(input=field_kind), mapping)
            outcomes.append(
                {
                    "id": identity,
                    "start": microseconds(start),
                    "end": microseconds(end),
                    "values": {"field": value} if value is not None else {},
                    "units": mapping.units if value is not None else {},
                }
            )
        except ValueError as error:
            outcomes.append({"code": str(error), "field": None})
        except TypeError:
            outcomes.append({"code": "invalid_row", "field": None})
    return outcomes


def fixtures():
    def rows(values):
        return [
            {"id": str(index), "start": DATE, "value": value} for index, value in enumerate(values)
        ]

    def json_case(label, kind, data, options=None):
        mapping = BASE | (options or {})
        encoded = json.dumps(data, ensure_ascii=True, allow_nan=False)
        return {
            "label": label,
            "mapping": mapping,
            "kind": kind,
            "data": encoded,
            "outcomes": process(encoded, FileMapping(**mapping), kind),
        }

    digits = [
        0,
        1,
        1.25,
        -2,
        100_000_000_000_000_000_000,
        9_007_199_254_740_993,
        "0,1",
        "0.1",
        "+.7",
        "-.7",
        "+7.",
        "1E-03",
        "1E+25",
        "1e+100",
        "1e101",
        "1e-100",
        "1e-101",
        "0e-101",
        "0.00e-99",
        "0e+100",
        "-0",
        "-0.0",
        "0005",
        "9007199254740993",
        "1.234567890123456789",
        "3.0",
        "3.01",
        "+0002e2",
        " 1",
        "1 ",
        "２",
        "Ⅳ",
        "1_000",
        "١,٢",
        True,
        False,
        None,
        "",
        "unknown",
        [],
        {},
        "Infinity",
        "1" * 100,
        "0" * 127 + "1",
        "0" * 128 + "1",
        "0.1.2",
        "1e+",
        "1e-",
        "1e1e1",
    ]
    for kind in ("integer", "number"):
        for dot in (".", ","):
            yield json_case(
                f"{kind}{dot}",
                kind,
                rows(digits),
                {"decimal_separator": dot, "units": {"field": "mg"}},
            )
    yield json_case(
        "booleans",
        "boolean",
        rows([True, False, "true", "false", "True", "False", "yes", 1, None, [], {}, ""]),
    )
    yield json_case(
        "text", "text", rows(["синтетика 🧪", "unknown", None, 15, {}, "\x00", "\ud800"])
    )
    yield json_case("complex", "json", rows([{}, [], False, None, "unknown"]))
    stamps = [
        DATE,
        "2026-03-29T02:35:00+01:00",
        "2026-03-29T03:10:00+00:00",
        "20260329T031000+0200",
        "2026-W13-7T03:10+02:00",
        "2026W137T0310+02",
        "2026-03-29🧪03:10:00,9192930+02:00",
        "20260329 03+02",
        "2026-03-29",
        "2026-03-29T03:10:00",
        "2026-03-29T25:10:00",
        "2026-02-30T03:10+02:00",
        "2026-03-29T03:10:00+020000",
        "2026-03-29T03:10:00+02:00:01",
        "2026-03-29T03:10:00+02:00:00.25",
        "2026-03-29T03:10:00z",
        "0",
        None,
        42,
        "2026-10-25T02:30:00+02:00",
        "2026-10-25T02:30:00+01:00",
        "2026-10-25T02:30:00+03:00",
    ]
    yield json_case(
        "timestamps",
        "text",
        [row | {"start": row["value"], "value": "synthetic"} for row in rows(stamps)],
    )
    yield json_case(
        "intervals",
        "text",
        [
            row | {"end": row["value"], "value": "synthetic"}
            for row in rows(
                [
                    "2026-03-29T04:10:00+02:00",
                    DATE,
                    "2026-03-29T03:00:00+02:00",
                    "",
                    None,
                ]
            )
        ],
        {"end_column": "end"},
    )
    repeated = rows(["ignored", "first", "follow-up", "other"])
    repeated[0] |= {"id": "same", "start": False}
    repeated[1]["id"] = "same"
    del repeated[2]["value"]
    repeated[3]["id"] = ""
    yield json_case("identities", "text", repeated)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["id", "start", "value"])
    writer.writeheader()
    writer.writerows(rows(["4,7", "0.5", "NA", "85,001", "001", "\x00", "1.234,567"]))
    mapping = BASE | {"format": "csv", "decimal_separator": ",", "null_markers": ["NA"]}
    yield {
        "label": "csv",
        "kind": "number",
        "mapping": mapping,
        "data": stream.getvalue(),
        "outcomes": process(stream.getvalue(), FileMapping(**mapping), "number"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(list(fixtures()), ensure_ascii=True), encoding="ascii")


if __name__ == "__main__":
    main()
