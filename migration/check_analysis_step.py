"""Run synthetic model steps through the pinned Python analysis loop."""

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from unicodedata import unidata_version
from unittest.mock import patch

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.agent import AgentStep, answer_question
from garmin_ai.config import Settings
from garmin_ai.diary_forms import urgent_notice
from garmin_ai.i18n import translate

TOOL = "personal_baseline"
READ = {"calls": [{"name": TOOL, "arguments_json": "{}"}]}
EVIDENCE = {"mean": 78}
CLAIM = {"evidence_id": 1, "path": ["mean"], "value": 78}
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


class Provider:
    def __init__(self, steps):
        self.steps = iter(steps)

    def structured(self, *_):
        return AgentStep.model_validate(next(self.steps))


def capture(output):
    cases = [
        ("supported", EVIDENCE, {"answer": "Synthetic verified result", "evidence_ids": [1]}),
        ("numeric_only", EVIDENCE, {"numeric_claims": [CLAIM], "evidence_ids": [1]}),
        ("no_evidence", None, {"answer": "Synthetic result", "evidence_ids": [1]}),
        ("unselected", EVIDENCE, {"answer": "Synthetic result", "evidence_ids": [2]}),
        ("no_citations", EVIDENCE, {"answer": "Synthetic result"}),
        (
            "tool_failed",
            {"error": "synthetic unavailable"},
            {"answer": "Synthetic result", "evidence_ids": [1]},
        ),
        (
            "invented_amount",
            EVIDENCE,
            {"numeric_claims": [{**CLAIM, "value": 87}], "evidence_ids": [1]},
        ),
        ("numeric_narrative", EVIDENCE, {"answer": "Synthetic 78", "evidence_ids": [1]}),
        ("numeric_unicode_narrative", EVIDENCE, {"answer": "Synthetic Ⅳ", "evidence_ids": [1]}),
        ("numeric_ideograph", EVIDENCE, {"answer": "Synthetic 四", "evidence_ids": [1]}),
        ("numeric_ideograph_scale", EVIDENCE, {"answer": "Synthetic 万", "evidence_ids": [1]}),
        ("numeric_ideograph_financial", EVIDENCE, {"answer": "Synthetic 壹", "evidence_ids": [1]}),
        (
            "numeric_ideograph_supplementary",
            EVIDENCE,
            {"answer": "Synthetic 𠄡", "evidence_ids": [1]},
        ),
        ("supported_ideographic_text", EVIDENCE, {"answer": "合成結果", "evidence_ids": [1]}),
        (
            "safety_precedes_tool",
            None,
            {"urgent_safety": True, "response_language": "en-US", **READ},
        ),
        ("nothing_to_answer", None, {}),
    ]
    engine = isolated_engine()
    results = []
    try:
        for name, evidence, final in cases:
            steps = [READ, final] if evidence is not None else [final]
            with (
                Session(engine) as session,
                patch("garmin_ai.agent.call_tool", return_value=evidence),
            ):
                actual = answer_question(
                    session, Provider(steps), "synthetic analysis question", Settings(), NOW
                )
                session.rollback()
            language = AgentStep.model_validate(final).response_language or "ru"
            known = {
                translate("agent.answer_unverified", language): "missing_evidence",
                translate("agent.numbers_unverified", language): "unverified",
                translate("agent.steps_exhausted", language): "empty",
                urgent_notice(language): "safety",
            }
            result = known.get(actual, "verified")
            if result == "verified":
                assert name in {"supported", "numeric_only", "supported_ideographic_text"} and (
                    "Synthetic" in actual or "78" in actual or "合成結果" in actual
                ), name
            results.append(
                {
                    "name": name,
                    "document": final,
                    "evidence": evidence,
                    "language": language,
                    "result": result,
                }
            )
    finally:
        engine.dispose()
    output.write_text(json.dumps(results, ensure_ascii=False))


def numeric_ranges(output):
    if unidata_version != "17.0.0":
        raise ValueError("Use the pinned Python Unicode data")
    points = [point for point in range(0x110000) if chr(point).isnumeric()]
    ranges = []
    for point in points:
        if ranges and point == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], point)
        else:
            ranges.append((point, point))
    digest = sha256(b"".join(point.to_bytes(4, "big") for point in points)).hexdigest()
    output.write_text(
        "// Generated from the pinned Python str.isnumeric() (Unicode 17.0.0).\n"
        "// Rebuild with migration/check_analysis_step.py --numeric-ranges.\n"
        "const NUMERIC_RANGES: &[(u32, u32)] = &[\n"
        + "".join(f"    (0x{start:x}, 0x{end:x}),\n" for start, end in ranges)
        + "];\n"
        + f'#[cfg(test)]\nconst NUMERIC_DIGEST: &str = "{digest}";\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--numeric-ranges", action="store_true")
    options = parser.parse_args()
    if options.numeric_ranges:
        numeric_ranges(options.output)
    else:
        capture(options.output)


if __name__ == "__main__":
    main()
