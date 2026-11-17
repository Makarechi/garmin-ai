"""Capture the original prompt and fragments used for reply safety checks."""

import argparse
import hashlib
import json
from pathlib import Path

from garmin_ai.agent import SafetyScreen, screen_reply_safety
from garmin_ai.llm import ProviderUnavailable

PATTERN = "aП🙂бé-"  # Synthetic multibyte positions around fragment boundaries.


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Provider:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def structured(self, instruction, prompt, schema):
        assert schema is SafetyScreen
        self.calls.append([digest(instruction), digest(prompt), len(prompt)])
        response = next(self.replies)
        if response is None:
            raise ProviderUnavailable("synthetic provider offline")
        return SafetyScreen.model_validate(response)


def capture(output):
    clear = {"urgent": False}
    russian = {"urgent": False, "response_language": "RU"}
    urgent = {"urgent": True, "response_language": "sk-SK"}
    cases = [
        ("short_clear", 17, "en", [clear]),
        ("short_urgent", 72, "en", [urgent]),
        ("short_error", 90, "en", [None]),
        ("exact_short_boundary", 16000, "en", [russian]),
        ("first_oversized_reply", 16001, "en", [russian, clear]),
        ("urgent_in_second_fragment", 36000, "en", [russian, urgent]),
        ("provider_stops_after_first", 36000, "en", [russian, None]),
        ("exact_coverage", 48000, "en", [clear, clear, clear, clear]),
        ("limited_coverage", 48001, "en", [russian, clear, clear, clear]),
        ("long_unchecked_tail", 100000, "en", [russian, clear, clear, clear]),
    ]
    artifact = []
    for name, length, locale, replies in cases:
        text = (PATTERN * (length // len(PATTERN) + 1))[:length]
        provider = Provider(replies)
        try:
            outcome = screen_reply_safety(provider, text, locale=locale)
            result = {
                "intent": outcome.intent,
                "language": outcome.response_language,
                "oversized_notice": outcome.clarification is not None,
            }
        except ProviderUnavailable:
            result = None
        artifact.append(
            {
                "name": name,
                "length": length,
                "locale": locale,
                "replies": replies,
                "calls": provider.calls,
                "result": result,
            }
        )
    output.write_text(json.dumps({"pattern": PATTERN, "cases": artifact}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)


if __name__ == "__main__":
    main()
