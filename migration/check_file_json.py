"""Capture synthetic JSON import outcomes from the pinned Python release."""

import argparse
import json
from pathlib import Path
from random import Random

from garmin_ai.file_source import MAX_FILE_BYTES, _digest, _jsonb_string_issue, _read_rows


def nested_json(opening, closing, depth, terminal=b"0"):
    return b'[{"val":' + opening * depth + terminal + closing * depth + b"}]"


def examples():
    yield from {
        "basic": b'[{"Id":"a","amount":3,"true":true,"missing":null}]',
        "bom": b"\xef\xbb\xbf[{}]",
        "double-bom": b"\xef\xbb\xbf\xef\xbb\xbf[{}]",
        "invalid-utf8": b'[{"x":"\xff"}]',
        "root-object": b'{"value":0}',
        "root-bool": b"true",
        "root-duplicate": b'{"v":0,"v":1}',
        "empty": b"[]",
        "row-null": b"[null]",
        "row-list": b"[{} , []]",
        "excess-with-late-error": b"[" + b"{}," * 503 + b'{"a":0,"a":1}]',
        "duplicate": b'[{"a":0,"a":1}]',
        "duplicate-by-escape": rb'[{"a":0,"\u0061":1}]',
        "duplicate-nested": rb'[{"x":[{"\ud83e\uddea":12,"\uD83E\uDDEA":0}]}]',
        "surrogate-duplicate": rb'[{"\ud800":12,"\uD800":0}]',
        "surrogate-and-private-scalar": '[{"\\ud800":0,"\U000f0000\ue000":1}]'.encode(),
        "marker-string": ' [{"\U000f0000":"x\U000f0000\U000f0000"}] '.encode(),
        "marker-escaped": rb'[{"\udb80\udc00":"\uDB80\uDC00\uDB80\uDC00"}]',
        "marker-duplicate": '[{"\\uDB80\\uDC00":0,"\U000f0000":1}]'.encode(),
        "paired-supplementary": rb'[{"\ud83e\uddea":"\uD83E\uDDEA"}]',
        "high-only": rb'[{"a":"\ud800"},{"a":"valid"}]',
        "low-only": rb'[{"a":"\udfff"}]',
        "high-high-low": rb'[{"a":"\ud800\ud83e\uddea"}]',
        "backslash-u-literal": rb'[{"a":"\\ud800","b":"\\uQQQQ"}]',
        "malformed-hex": rb'[{"a":"\uQQQQ"}]',
        "escaped-quote": rb'[{"x":"\\\"\ud800"}]',
        "escaped-keys": rb'[{"a\u0000":"safe"},{"\ud800":"safe"}]',
        "nul-before-surrogate-same-string": rb'[{"a":"\ud800\u0000"}]',
        "nested-later-unicode": rb'[{"a":"\u0000","b":[{"x":["\ud800"]}]}]',
        "nested-later-escaped-nul": rb'[{"x":[{"a":["\ud800",{"z":"\u0000"}]}]}]',
        "values-before-keys": rb'[{"\ud800":"\u0000","x":1}]',
        "key-order-before-sorting": rb'[{"z\u0000":1,"a\ud800":2}]',
        "raw-control-not-an-escape": b'[{"value":"abc\x00"}]',
        "nan": b'[{"x":NaN}]',
        "infinity": b'[{"x":Infinity}]',
        "minus-infinity": b'[{"x":-Infinity}]',
        "overflow": b'[{"x":1e309}]',
        "underflow": b'[{"x":1e-1000}]',
        "signed-zero": b'[{"x":-0, "a":-0.0000, "b":-0e+12}]',
        "integer-precision": b'[{"x":18446744073709551616,"y":-9223372036854775809}]',
        "float-precision": b'[{"x":9007199254740993.0}]',
        "float-decimal-precision": b'[{"x":1.000000000000000001}]',
        "float-exact-decimal": b'[{"x":0.001230000,"y":12.3e-004,"z":1.230000e-3}]',
        "shortest-float-cutoffs": b'[{"a":1.0e15,"b":1e16,"c":1e-4,"d":1e-5,"e":1e-6,"f":5e-324}]',
        "leading-zero": b'[{"x":01}]',
        "missing-trailing-digit": b'[{"x":123.}]',
        "trailing-comma": b"[{},]",
        "trailing-bytes": b"[{}]{}",
        "digits-at-limit": b'[{"value":' + b"7" * 4300 + b"}]",
        "digits-over-limit": b'[{"value":' + b"7" * 4301 + b"}]",
        "long-valid-coefficient": b'[{"x":1' + b"0" * 5000 + b'e-5000,"y":{"x":1}}]',
        "long-lossy-coefficient": b'[{"x":1' + b"0" * 5000 + b"1e-5001}]",
        "long-invalid-coefficient": b'[{"x":1' + b"0" * 5000 + b".}]",
        "deep": b'[{"val":' + b"[" * 900 + b"0" + b"]" * 900 + b"}]",
        "deep-1200": nested_json(b"[", b"]", 1_200),
        "deep-array-42000": nested_json(b"[", b"]", 42_000),
        "deep-array-44000": nested_json(b"[", b"]", 44_000),
        "deep-object-28000": nested_json(b'{"v":', b"}", 28_000),
        "deep-object-30000": nested_json(b'{"v":', b"}", 30_000),
        "deep-mixed-17000": nested_json(b'[{"v":', b"}]", 17_000),
        "deep-mixed-18000": nested_json(b'[{"v":', b"}]", 18_000),
        "deep-malformed": nested_json(b"[", b"]", 42_000)[:-1],
        "deep-duplicate": nested_json(b"[", b"]", 42_000)[:-2] + b',"val":1}]',
        "deep-nul": nested_json(b"[", b"]", 42_000, rb'"\u0000"'),
        "too-deep": b'[{"val":' + b"[" * 100_000 + b"0" + b"]" * 100_000 + b"}]",
        "too-many": b"[" + b"{}," * 500 + b"{}]",
        "rows-at-limit": b"[" + b"{}," * 499 + b"{}]",
        "dense": b'[{"value":[' + b"0," * 250_000 + b"0]}]",
        "largest-string": b'[{"x":"' + b"a" * (MAX_FILE_BYTES - 10) + b'"}]',
        "too-large": b'[{"x":"' + b"a" * (MAX_FILE_BYTES - 9) + b'"}]',
    }.items()

    random = Random(20261117)
    for index in range(200):
        document = [
            {
                "case": index,
                "as_float": random.uniform(-10, 10) * 10.0 ** random.randrange(-300, 300),
                "as_integer": random.getrandbits(random.randrange(0, 360)),
                "contains": random.choice(["\x00", "\U000f0000", "é 🧪", "\\\t", "", "\udfff"]),
                "array": [True, None, {"left": "a", "right": "b"}],
            }
        ]
        yield (
            f"generated-{index}",
            json.dumps(document, ensure_ascii=index % 2 == 0).encode("utf-8", "backslashreplace"),
        )


def capture(root):
    root.mkdir(parents=True, exist_ok=True)
    cases = []
    for name, body in examples():
        path = root / (name + ".json")
        path.write_bytes(body)
        try:
            if len(body) > MAX_FILE_BYTES:
                raise ValueError("File or mapping exceeds the local size limit")
            rows = _read_rows(body.decode("utf-8-sig"), "json")
            outcome = {
                "rows": [
                    {"issue": issue}
                    if (issue := _jsonb_string_issue(row))
                    else {"canonical_sha256": _digest(row)}
                    if name.startswith("deep")
                    else {"valid": True}
                    if name in {"dense", "largest-string"}
                    else {"row": row}
                    for row in rows
                ]
            }
        except UnicodeError:
            outcome = {"error": "File must use UTF-8"}
        except ValueError as error:
            message = str(error)
            if "integer string conversion" in message:
                message = "Invalid CSV or JSON file"
            outcome = {"error": message}
        cases.append({"file": path.name, "expected": outcome})
    (root / "cases.json").write_text(json.dumps(cases, ensure_ascii=True), encoding="utf-8")
    print(f"Captured {len(cases)} JSON import outcomes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    capture(parser.parse_args().directory)
