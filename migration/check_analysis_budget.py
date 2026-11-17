"""Capture shared interpretation and answer budgets from pinned Python."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from garmin_ai.agent import AgentStep, AnalysisBudget, Interpretation


def call(stage, count=0, *, fill="x", elapsed_ms=0, instruction=""):
    return {
        "stage": stage,
        "fill": fill,
        "length": count,
        "elapsed_ms": elapsed_ms,
        "instruction": instruction,
    }


def capture(output):
    groups = {
        "shared_call_count": [call("interpretation")] + [call("answer")] * 6,
        "interpretation_byte_boundary": [
            call("interpretation", 85_976),
            call("interpretation", 85_977),
            call("interpretation", 85_974, instruction="я"),
            call("interpretation", 85_975, instruction="я"),
        ],
        "answer_byte_boundary": [
            call("answer", 93_660),
            call("answer", 93_661),
            call("answer", 46_830, fill="я"),
            call("answer", 46_831, fill="я"),
        ],
        "total_input_boundary": [call("interpretation", 85_976)]
        + [call("answer", 93_660)] * 3
        + [call("answer")],
        "time_boundary": [call("answer", elapsed_ms=119_999), call("answer", elapsed_ms=120_000)],
        "rejected_calls_do_not_spend_budget": [call("interpretation", 85_977)]
        + [call("answer")] * 6,
    }
    schemas = {"interpretation": Interpretation, "answer": AgentStep}
    results = []
    for name, calls in groups.items():
        with patch("garmin_ai.agent.monotonic", return_value=0.0):
            budget = AnalysisBudget()
        actual = []
        for spec in calls:
            elapsed = spec["elapsed_ms"] / 1_000
            with patch("garmin_ai.agent.monotonic", return_value=elapsed):
                accepted = budget.consume(
                    spec["instruction"], spec["fill"] * spec["length"], schemas[spec["stage"]]
                )
            actual.append(
                {
                    **spec,
                    "result": "accepted" if accepted else "timeout" if elapsed >= 120 else "limit",
                }
            )
        results.append({"name": name, "calls": actual})
    output.write_text(json.dumps(results, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)


if __name__ == "__main__":
    main()
