"""Freeze the explicit chat-field parser's accepted documents and refusals."""

import argparse
import itertools
import json
from pathlib import Path

from garmin_ai.tracker_chat_setup import _field


def cases():
    result = [
        "",
        "Title",
        "| text",
        "Title |",
        "Title | text | other",
        "Title | text extra",
        "Title | boolean extra",
        "Title | choice",
        "Title | choice A, A",
        "Title | choice A, ,B",
        "Title | number mg",
        "Title | count 0-" + "9" * 100,
        "Title | count 0-" + "0" * 101,
        "Title | count 0-" + "0" * 99 + "1",
    ]
    for label, kind in itertools.product(
        ["Synthetic rating", "Синтетическая оценка", "[]_*<>!|", "A" * 120, "A" * 121],
        ["text", "ТЕКСТ", "yes/no", "BOOLEAN", "да/нет", "choice A, B", "выбор 🧪, Ещё"],
    ):
        result.append(label + " | " + kind)
    for kind, bounds, whitespace in itertools.product(
        ["scale", "шкала", "count", "счётчик", "счетчик", "ſcale"],
        [
            "1-5",
            "-2--1",
            "0–20",
            "0-21",
            "2-1",
            "0-9007199254740992",
            "0-9007199254740993",
            "-9007199254740992--9007199254740991",
            "١–٥",
            "０-９",
            "+1-5",
            "1.0-5",
            "1e0-5",
            "1 - 5",
            "1---5",
        ],
        [" ", "\t", "\u001c"],
    ):
        result.append("Synthetic bound |" + whitespace + kind + whitespace + bounds)
    result += ["Choice | choice " + ",".join(map(str, range(count))) for count in [1, 50, 51]]
    result += ["Choice | choice " + "A" * length for length in [120, 121]]
    return result


def capture():
    result = []
    for text in cases():
        try:
            expected = _field(text.strip()).model_dump(mode="json")
        except (ValueError, OverflowError):
            expected = None
        result.append([text, expected])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(capture(), ensure_ascii=False, indent=2) + "\n")
