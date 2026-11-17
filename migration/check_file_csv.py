"""Generate synthetic strict CSV cases with the pinned Python file adapter."""

import argparse
import json
from pathlib import Path
from random import Random

from garmin_ai.file_source import MAX_FILE_BYTES, _read_bounded, _read_rows


def examples():
    cases = {
        "ordinary": b"id,amount\na,1\nb,0",
        "bom": b"\xef\xbb\xbfid,amount\rzero,0\r",
        "repeated-bom": b"\xef\xbb\xbf\xef\xbb\xbfid\na",
        "header-empty-field": b'""\n""',
        "header-blank": b"\nname\nvalue",
        "blank-rows": b"a,b\r\n\n\r\nzero,\r\n\r\n0,1\r",
        "missing-last": b"a,b\nhello",
        "empty-last": b"a,b\nhello,",
        "extra-last": b"a,b\nhello,world,",
        "duplicate-header": b'a,"a"\n0,1',
        "all-empty-row": b"x,y\n,",
        "no-rows": b"x,y\n\r\n",
        "nul-value": b"x,y\nabc\x00xyz,other",
        "invalid-utf8": b"x,y\n1,\xff",
        "escaped-quotes": b'a,b\n"quoted,""escaped""",0',
        "lines-inside-quotes": b'a,b\r\n"line1\r\nline2\rline3\nline4",0',
        "quote-in-unquoted": b'a,b\na"b,c',
        "unexpected-after-quote": b'a,b\n"a" ,b',
        "missing-closing-quote": b'a,b\n"a,b',
        "width-then-syntax": b'a,b\none\n"unfinished',
        "rows-at-limit": b"x\n" + b"a\n" * 500,
        "too-many-rows": b"x\n" + b"a\n" * 501,
        "row-width-on-last": b"x\n" + b"a\n" * 500 + b"a,b",
        "malformed-after-first-excess-row": b"x\n" + b"a\n" * 501 + b'"not parsed',
        "long-ascii": b"x\n" + b"a" * 131_072,
        "too-long-ascii": b"x\n" + b"a" * 131_073,
        "long-unicode": ("x\n" + "é" * 131_072).encode(),
        "too-long-unicode": ("x\n" + "é" * 131_073).encode(),
        "same-wide-header-many-rows": b"f" * 131_072 + b"\n" + b"1\n" * 500,
    }
    exact = b"x\n" + (b"v" * 1023 + b"\n") * 499
    exact += b"v" * (MAX_FILE_BYTES - len(exact))
    cases["bytes-at-limit"] = exact
    cases["beyond-byte-limit"] = exact + b"v"
    yield from cases.items()
    random = Random(2_603)
    alphabet = ("A", " ", "1", "é", "🧪", ",", "\r", "\n", '"', "\x00")
    for index in range(260):
        text = random.choice(("", "x\n", "x,y\r\n"))
        text += "".join(random.choices(alphabet, k=random.randrange(0, 48)))
        yield f"generated-{index}", text.encode()


def capture(root):
    root.mkdir(parents=True, exist_ok=True)
    cases = []
    for name, body in examples():
        path = root / (name + ".csv")
        path.write_bytes(body)
        try:
            text = _read_bounded(path, MAX_FILE_BYTES).decode("utf-8-sig")
            rows = _read_rows(text, "csv")
            expected = {"headers": list(rows[0]), "rows": [list(row.values()) for row in rows]}
        except UnicodeError:
            expected = {"error": "File must use UTF-8"}
        except ValueError as error:
            expected = {"error": str(error)}
        cases.append({"file": path.name, "expected": expected})
    (root / "cases.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    print(f"Captured {len(cases)} strict CSV outcomes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    capture(parser.parse_args().directory)
