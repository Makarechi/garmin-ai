"""Generate synthetic file mapping and confirmation decisions from pinned Python."""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from garmin_ai.file_source import FileMapping, _digest

SOURCE = "f" * 64
SCHEMA = "e" * 64
VERSION = "11111111-1111-4111-8111-111111111111"
BASE = {
    "format": "csv",
    "source_instance_id": "kept-source",
    "device_id": "my_device1",
    "definition_key": "user.synthetic",
    "row_id_column": "ID",
    "start_column": "clock",
    "timezone": "Europe/Bratislava",
    "field_columns": {"z": "Конец", "a": "start"},
    "decimal_separator": ",",
    "null_markers": ["", "∅"],
}


def example_inputs():
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    yield encode(BASE)
    yield encode(BASE | {"format": "json", "end_column": "received", "units": {"z": "мг"}})
    yield encode([])
    yield b""
    yield b"\xff"
    yield b"\xef\xbb\xbf" + encode(BASE)
    yield encode(BASE | {"unknown_value": "synthetic-private-input"})
    for key, values in {
        "format": ["Csv", False, "json", "tsv"],
        "source_instance_id": ["a", "A", "a" * 63, "a" * 64, "a-z", "с", "a\n"],
        "device_id": [100, None, "1", "a1", "a_1", "a.1"],
        "definition_key": ["user.a", "user.A", "user.a_b", "user.a-b", "user.a.b", "tracker.a"],
        "row_id_column": ["", "Я" * 100, "🧪" * 101, 2, None],
        "start_column": [" ", "{}", "\x00", None, "a" * 101],
        "end_column": ["", None, False, "Ä" * 100, "Ä" * 101],
        "timezone": ["UTC", "Europe/Kyiv", "Europe/Kiev", "Etc/GMT+3", "invalid/zone", "utc", ""],
        "field_columns": [
            {},
            {"x": 1},
            {"x": ""},
            {"x": "é" * 100},
            {"x": "é" * 101},
            {str(i): "same" for i in range(32)},
            {str(i): "same" for i in range(33)},
        ],
        "units": [None, [], {"x": 10}, {"x": "mL"}, {}, {str(i): "u" for i in range(33)}],
        "decimal_separator": [".", "", "::", "∴", 0, None],
        "null_markers": [
            [],
            ["", ""],
            ["x", "X"],
            ["é", "é"],
            [1],
            None,
            [str(i) for i in range(16)],
            [str(i) for i in range(17)],
        ],
    }.items():
        for value in values:
            yield encode(BASE | {key: value})
    for key in BASE:
        yield encode({name: value for name, value in BASE.items() if name != key})
    yield encode(BASE).replace(b'"format":"csv"', b'"format":"invalid","format":"csv"')
    yield encode(BASE | {"field_columns": {"z": "old", "a": "middle"}}).replace(
        b'"a":"middle"', b'"a":"middle","z":"last"'
    )
    data = encode(BASE)
    exact = data + b" " * (16_000 - len(data))
    yield exact
    yield exact + b" "


def expected(content):
    if len(content) > 16_000:
        return {"error": "File or mapping exceeds the local size limit"}
    try:
        mapping = FileMapping.model_validate_json(content)
    except ValidationError:
        return {"error": "Invalid mapping file"}
    document = mapping.model_dump(mode="json")
    return {
        "document": document,
        "ordered_fields": list(mapping.field_columns),
        "confirmation": _digest(
            {
                "file_hash": SOURCE,
                "mapping": document,
                "schema_hash": SCHEMA,
                "definition_version_id": VERSION,
            }
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    cases = [[list(content), expected(content)] for content in example_inputs()]
    parser.parse_args().output.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    print(f"Captured {len(cases)} synthetic mapping decisions")
