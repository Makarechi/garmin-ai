"""Summarize synthetic-only evaluation metadata without claiming acceptance.

No inputs, prompts, responses, keys, or user data belong in the report.
"""

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

LANGUAGES = ("en", "ru", "sk", "uk", "de", "es", "fr", "pt-BR", "ja", "zh-Hans", "ar", "hi")
PROVIDERS = ("openai", "gemini", "anthropic")
WORKFLOWS = ("read", "create", "clarify_time", "correct_undo", "unsafe_refusal")
LEVELS = {"synthetic", "live"}
OUTCOMES = {"pass", "fail", "skipped"}
REASONS = {"not_configured", "unsupported", "provider_error", "validation_failed", "other"}
FIELDS = {
    "case_id",
    "provider",
    "model",
    "language",
    "workflow",
    "level",
    "outcome",
    "reason",
    "observed_at",
    "commit",
    "prompt_sha256",
    "schema_sha256",
    "tools_sha256",
}
_CASE_FILE = Path(__file__).with_name("acceptance_cases.json")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def load_cases():
    cases = json.loads(_CASE_FILE.read_text(encoding="utf-8"))
    expected = {f"AT-{number:02d}" for number in range(1, 41)}
    if (
        not isinstance(cases, list)
        or any(not isinstance(case, dict) or set(case) != {"id", "name", "area"} for case in cases)
        or len(cases) != len(expected)
        or {case["id"] for case in cases} != expected
        or any(
            not isinstance(case["name"], str)
            or not case["name"]
            or not isinstance(case["area"], str)
            or not case["area"]
            for case in cases
        )
    ):
        raise ValueError("Invalid acceptance case catalog")
    return cases


def validate_record(record, cases):
    if not isinstance(record, dict) or set(record) != FIELDS:
        raise ValueError("Invalid evidence fields")
    if any(not isinstance(record[field], str) for field in FIELDS - {"reason"}):
        raise ValueError("Invalid evidence fields")
    if record["reason"] is not None and not isinstance(record["reason"], str):
        raise ValueError("Invalid evidence fields")
    if (
        record["case_id"] not in cases
        or record["provider"] not in PROVIDERS
        or record["language"] not in LANGUAGES
        or record["workflow"] not in WORKFLOWS
        or record["level"] not in LEVELS
        or record["outcome"] not in OUTCOMES
        or not isinstance(record["model"], str)
        or not _MODEL.fullmatch(record["model"])
    ):
        raise ValueError("Invalid evidence classification")
    if (record["outcome"] == "pass" and record["reason"] is not None) or (
        record["outcome"] != "pass" and record["reason"] not in REASONS
    ):
        raise ValueError("Invalid outcome reason")
    if not isinstance(record["commit"], str) or not _COMMIT.fullmatch(record["commit"]):
        raise ValueError("Invalid commit")
    for field in ("prompt_sha256", "schema_sha256", "tools_sha256"):
        if not isinstance(record[field], str) or not _SHA.fullmatch(record[field]):
            raise ValueError("Invalid evidence digest")
    try:
        observed = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
        if observed.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid evidence timestamp") from None


def summarize(records):
    cases = {case["id"] for case in load_cases()}
    if not isinstance(records, list):
        raise ValueError("Evidence must be a list")
    counts = {}
    for record in records:
        validate_record(record, cases)
        key = (record["provider"], record["language"], record["workflow"])
        counts.setdefault(key, {}).setdefault(record["model"], {}).setdefault(
            record["case_id"], Counter()
        )[(record["level"], record["outcome"])] += 1
    cells = []
    for provider in PROVIDERS:
        for language in LANGUAGES:
            for workflow in WORKFLOWS:
                observed = counts.get((provider, language, workflow), {})
                cells.append(
                    {
                        "provider": provider,
                        "language": language,
                        "workflow": workflow,
                        "models": {
                            model: {
                                case_id: {
                                    level: {
                                        outcome: outcomes[(level, outcome)]
                                        for outcome in sorted(OUTCOMES)
                                    }
                                    for level in sorted(LEVELS)
                                }
                                for case_id, outcomes in sorted(case_outcomes.items())
                            }
                            for model, case_outcomes in sorted(observed.items())
                        },
                        "status": "results_recorded" if observed else "unverified",
                    }
                )
    return {
        "cases": sorted(cases),
        "cells": cells,
        "acceptance": "not_assessed",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="JSON list of synthetic-only result metadata")
    args = parser.parse_args()
    try:
        records = json.loads(args.evidence.read_text(encoding="utf-8"))
        report = summarize(records)
    except (ValueError, OSError):
        parser.exit(2, "Invalid or unreadable evaluation metadata.\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
