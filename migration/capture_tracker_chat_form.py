"""Freeze deterministic tracker chat answers, prompts and local-time refusals."""

import argparse
import base64
import hashlib
import itertools
import json
from datetime import UTC, datetime
from decimal import InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import TZPATH, available_timezones

from garmin_ai.tracker_chat_form import (
    FormAnswerError,
    _choice_labels,
    _prompt,
    _time,
    _value,
    begin_chat_form,
)
from garmin_ai.tracker_forms import ActionSpec, FormFieldSpec, FormSpec


def rust_decimal(code):
    return format(code, "_d") if code >= 100_000 else str(code)


def capture_unicode():
    rows = []
    digits = []
    for code in range(0x110000):
        text = chr(code)
        if text.casefold() != text:
            escaped = "".join("\\u{" + format(ord(c), "x") + "}" for c in text.casefold())
            rows.append(f'    (0x{code:x}, "{escaped}"),')
        if text.isdigit() and not text.isdecimal():
            digits.append(code)
    s = "// Generated from the frozen CPython Unicode case-fold and digit behavior.\n"
    s += "pub(super) const CASEFOLD: &[(u32, &str)] = &[\n" + "\n".join(rows) + "\n];\n"
    s += "pub(super) const EXTRA_DIGITS: &[u32] = &[" + ",".join(map(rust_decimal, digits)) + "];\n"
    ranges = []
    first = None
    for code in range(0x110001):
        invalid = code < 0x110000 and not chr(code).isprintable()
        if invalid and first is None:
            first = code
        elif not invalid and first is not None:
            ranges.append((first, code - 1))
            first = None
    s += (
        "pub(super) const NONPRINTABLE: &[(u32,u32)] = &[\n"
        + "".join(f"    ({rust_decimal(first)}, {rust_decimal(last)}),\n" for first, last in ranges)
        + "];\n"
    )
    return s


def capture_forms():
    variants = [
        [],
        [{"input": "text", "min_length": 2, "max_length": 12, "unit": "synthetic_unit"}],
        [{"input": "choice", "options": ["", " ", "/skip", "=", "Straße"]}],
        [{"input": "choice", "options": [True, "True", None, "None", 1, "1"]}],
        [{"input": "json"}],
        [{"input": "boolean", "has_const": True, "const_value": True}],
        [
            {
                "input": "integer",
                "has_const": True,
                "const_value": 2,
                "validation_schema": {"const": 1},
            }
        ],
        [{"input": "integer", "minimum": 2, "maximum": 1}],
        [{"input": "integer", "minimum": 1.1, "maximum": 1.9}],
        [
            {
                "input": "number",
                "minimum": 0.1,
                "maximum": 0.10000000000000002,
                "exclusive_minimum": True,
                "exclusive_maximum": True,
            }
        ],
        [{"input": "text", "min_length": 4097}],
        [{"input": "text", "min_length": 10, "max_length": 9}],
        [{"input": "choice", "options": ["x" * 4097]}],
        [{"input": "json", "min_json_length": 4097}],
        [{"input": "json", "complex_json": True}],
        [{"input": "json", "min_json_storage_length": 65536}],
        [{"input": "choice", "options": ["no"], "validation_schema": {"const": "yes"}}],
        [{"input": "text", "required": False, "has_const": True, "const_value": "literal"}],
        [{"input": "text", "required": False, "complex_json": True}],
    ]
    records = []
    for _index, (kind, topology, locale, variant) in enumerate(
        itertools.product(
            ["create_entry", "edit_entry"],
            ["point", "open_interval", "bounded_interval", "flexible"],
            ["en", "ru"],
            variants,
        )
    ):
        identity = (
            "edit:00000000-0000-4000-8000-000000000002:1"
            if kind == "edit_entry"
            else "create:00000000-0000-4000-8000-000000000001"
        )
        fields = [
            FormFieldSpec(
                **{
                    "name": "value",
                    "field_id": "synthetic-value",
                    "label": "Synthetic [value]!",
                    "required": True,
                    **field,
                }
            )
            for field in variant
        ]
        form = FormSpec(
            id=identity,
            action=ActionSpec(
                id=identity,
                kind=kind,
                label="Synthetic",
                definition_key="user.synthetic",
                definition_version_id=UUID(int=1),
                form_id=identity,
            ),
            title="Synthetic",
            topology=topology,
            schema_hash="a" * 64,
            submission_id="b" * 32 if kind == "create_entry" else None,
            fields=fields,
            initial_values={"value": "old"} if kind == "edit_entry" else {},
            initial_units={},
            initial_start=datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
            if kind == "edit_entry"
            else None,
            initial_timezone="Europe/Bratislava" if kind == "edit_entry" else None,
        )
        pending = SimpleNamespace(value={"question": "Synthetic"})
        try:
            reply = begin_chat_form(pending, form, timezone="Asia/Kathmandu", locale=locale)
            state = pending.value["chat_form"]
            prompts = []
            count = 1 + (topology != "point") + len(state["field_order"])
            for step in range(count):
                state["step"] = step
                prompts.append(
                    _prompt(form, step, state["field_order"], locale=locale, state=state)
                )
            state["step"] = 0
            outcome = {"state": state, "reply": reply, "prompts": prompts}
        except FormAnswerError as error:
            outcome = {"answer_error": str(error)}
        except (ValueError, OverflowError):
            outcome = {"invalid": True}
        records.append(
            {
                "form": form.model_dump(mode="json"),
                "timezone": "Asia/Kathmandu",
                "locale": locale,
                "outcome": outcome,
            }
        )
    return records


def capture():
    fields = [
        {"input": "text", "min_length": 1, "max_length": 12},
        {"input": "text", "required": False, "min_length": 0},
        {"input": "integer", "minimum": -2, "maximum": 5},
        {"input": "integer", "minimum": -2.5, "maximum": 5.5, "exclusive_minimum": True},
        {"input": "number", "minimum": -2, "maximum": 5},
        {"input": "number"},
        {"input": "boolean"},
        {
            "input": "choice",
            "options": [{"x": [True, None, "\u00a0", "'"]}, ["a", 1], ["x", "y"], {"b": 2, "a": 1}],
        },
        {"input": "choice", "options": ["", " ", "/skip", "=", "Alpha", "ALPHA", "Straße"]},
        {"input": "choice", "options": [True, "True", None, "None", 1, "1"]},
        {"input": "choice", "options": ["yes", "no"], "validation_schema": {"const": "yes"}},
        {"input": "json"},
    ]
    answers = [
        "",
        " ",
        "-",
        "/skip",
        "=/skip",
        "/empty",
        "=/empty",
        "==/empty",
        "===/empty",
        "=",
        "==",
        "ordinary text",
        "x" * 13,
        "nul\x00text",
        "0",
        "-0",
        "1",
        "--1",
        "+1",
        "١",
        "²",
        "1_0",
        "1__0",
        "0.1",
        "1,5",
        "1.0",
        "1e0",
        "1e1000",
        "1e1001",
        "1e4095",
        "1e4096",
        "1e999999999",
        "1e999999999999999999999",
        "1e-9223372036854775808",
        "0e999999999999999999999",
        "1e-9999",
        "0.1234567890123456789",
        "NaN",
        "Infinity",
        "yes",
        "NO",
        "да",
        "Нет",
        "true",
        "false",
        "Alpha",
        "aLpHa",
        "STRASSE",
        "{'x': [True, None, '\\xa0', \"'\"]}",
        "['a', 1]",
        "{'b': 2, 'a': 1}",
        "1: True",
        "2: True",
        "3: null",
        "4: None",
        "5: 1",
        "6: 1",
        "null",
        "[]",
        "{}",
        '{"x":1,"x":2}',
        '{"x":0.1}',
        '{"x":1e1000}',
        '{"x":"\\u0000"}',
        '"\\ud800"',
        "[1.0,1e2,0.5]",
    ]
    values = []
    for field, answer, locale in itertools.product(fields, answers, ["en", "ru", "zz"]):
        field = (
            FormFieldSpec(
                name="value", field_id="synthetic-value", label="Synthetic", required=True, **field
            )
            if "required" not in field
            else FormFieldSpec(name="value", field_id="synthetic-value", label="Synthetic", **field)
        )
        try:
            outcome = {"value": _value(answer, field, locale)}
        except FormAnswerError as error:
            outcome = {"answer_error": str(error)}
        except (ValueError, OverflowError, RecursionError):
            outcome = {"invalid": True}
        except InvalidOperation:
            outcome = {"runtime_error": "InvalidOperation"}
        values.append(
            {
                "field": field.model_dump(mode="json"),
                "answer": answer,
                "locale": locale,
                "outcome": outcome,
            }
        )
    times = []
    now = datetime(2026, 11, 19, 10, tzinfo=UTC)
    for timezone, answer, locale in itertools.product(
        ["UTC", "Europe/Bratislava", "America/New_York", "Australia/Lord_Howe", "Asia/Kathmandu"],
        [
            "now",
            "СЕЙЧАС",
            "2026-11-19 10:00Z",
            "2026-11-19 10:05Z",
            "2026-11-19 10:06Z",
            "2026-02-30 12:00",
            "2026-11-19 10:00",
            "2026-11-19T10:00+05:45",
            "2026-03-08 02:30",
            "2026-11-01 01:30",
            "2026-03-29 02:30",
            "2026-10-25 02:30",
            "2026-10-04 02:15",
            "2026-04-05 01:45",
            "２０２６-１１-１９ １０:００",
            "2026-11-19 10:00:00",
            "0000-01-01 00:00",
            "0001-01-01 00:00",
            "0001-01-01 00:00+05:45",
            "0001-01-01 00:00-05:00",
            "9999-12-31 23:59",
            "9999-12-31 23:59-05:00",
            "9999-12-31 23:59+05:45",
            "2026-11-19 10:00+00:99",
        ],
        ["en", "ru"],
    ):
        try:
            outcome = {"value": _time(answer, timezone, now, locale).isoformat()}
        except FormAnswerError as error:
            outcome = {"answer_error": str(error)}
        except (ValueError, OverflowError):
            outcome = {"invalid": True}
        times.append(
            {
                "timezone": timezone,
                "answer": answer,
                "locale": locale,
                "now": now.isoformat(),
                "outcome": outcome,
            }
        )
    calendar_answers = [
        "0001-01-01 00:00",
        "0001-01-01 00:00+05:45",
        "9999-12-31 23:59",
        "9999-12-31 23:59Z",
        "9999-12-31 23:59-05:00",
        "9999-12-31 23:59+05:45",
    ]
    calendar = []
    timezone_data = {}
    for timezone in sorted(available_timezones()):
        outcomes = []
        for text in calendar_answers:
            try:
                outcome = {"value": _time(text, timezone, now, "en").isoformat()}
            except FormAnswerError as error:
                outcome = {"answer_error": str(error)}
            except (ValueError, OverflowError):
                outcome = {"invalid": True}
            outcomes.append(outcome)
        tzif = next(
            (Path(directory) / timezone).read_bytes()
            for directory in TZPATH
            if (Path(directory) / timezone).is_file()
        )
        digest = hashlib.sha256(tzif).hexdigest()
        timezone_data[digest] = base64.b64encode(tzif).decode("ascii")
        calendar.append({"timezone": timezone, "outcomes": outcomes, "tzif_hash": digest})
    return {
        "values": values,
        "calendar_answers": calendar_answers,
        "calendar": calendar,
        "timezone_data": timezone_data,
        "forms": capture_forms(),
        "times": times,
        "labels": [
            {"options": field["options"], "labels": _choice_labels(field["options"])}
            for field in fields
            if field["input"] == "choice"
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--unicode-output", type=Path)
    args = parser.parse_args()
    if args.unicode_output:
        args.unicode_output.write_text(capture_unicode())
    args.output.write_text(json.dumps(capture(), ensure_ascii=False, separators=(",", ":")) + "\n")
