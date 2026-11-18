"""Record the pinned local safety classifier for the existing synthetic fixtures."""

import argparse
import ast
import json
from pathlib import Path

from garmin_ai.diary_forms import (
    obvious_third_party_emergency,
    obvious_urgent_symptoms,
    urgent_notice,
)

NAMES = {
    "test_local_urgent_screen_handles_emergencies_without_negated_choices",
    "test_local_urgent_screen_handles_negation_without_masking_affirmative_phrases",
}


def cases():
    source = ast.parse(Path("tests/test_tracker_chat_form.py").read_text())
    texts = {
        "У мамы инсульт был ٢ года назад",
        "У мамы инсульт был ٠١ год назад",
        "У мамы инсульт был в 20١٠ году",
        "У мамы инсульт был ٢ года назад, а сегодня снова приступ",
        "My father had a stroke ２ years ago",
        "My father had a stroke ０１ year ago",
        "My father had a stroke in 20１０",
    }
    for test in source.body:
        if not isinstance(test, ast.FunctionDef):
            continue
        assertions = [
            call
            for item in ast.walk(test)
            if isinstance(item, ast.Assert)
            for call in ast.walk(item.test)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "obvious_urgent_symptoms"
        ]
        for call in assertions:
            argument = call.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                texts.add(argument.value)
        if assertions and test.name in NAMES:
            for item in ast.walk(test):
                if isinstance(item, ast.For) and isinstance(item.iter, ast.Tuple):
                    texts.update(
                        value.value
                        for value in item.iter.elts
                        if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    )
    assert len(texts) > 150
    return {
        "texts": [
            {
                "text": text,
                "urgent": obvious_urgent_symptoms(text),
                "third_party": obvious_third_party_emergency(text),
            }
            for text in sorted(texts)
        ],
        "messages": {locale: urgent_notice(locale) for locale in ("ru", "en", "ru-RU")},
    }


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("output", type=Path)
    output = cli.parse_args().output
    output.write_text(json.dumps(cases(), ensure_ascii=False))
