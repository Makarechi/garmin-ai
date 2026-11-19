"""Freeze aggregate model copies and fixed-offset date arithmetic from Python."""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from garmin_ai.generic_analytics import AnalysisSpec, spec_hash


def duration_micros(value):
    return (value.days * 86400 + value.seconds) * 1000000 + value.microseconds


def micros(at):
    naive = at.replace(tzinfo=None) - datetime(1970, 1, 1)
    return duration_micros(naive) - duration_micros(at.utcoffset())


def capture(spec):
    result = {
        "document": spec.model_dump(mode="json"),
        "hash": spec_hash(spec),
        "start": micros(spec.start),
        "end": micros(spec.end),
        "cutoff": micros(spec.knowledge_cutoff),
        "shifts": {},
    }
    for seconds in [0, 1, 3600, -3600, 86400, 9223372036854775807, -9223372036854775808]:
        try:
            shifted = spec.start - timedelta(seconds=seconds)
            result["shifts"][str(seconds)] = {"value": micros(shifted)}
        except OverflowError:
            result["shifts"][str(seconds)] = {"error": "OverflowError"}
    return result


def run(source, output):
    cases = []
    for original in json.loads(source.read_text(encoding="utf-8")):
        if "error" in original:
            continue
        spec = AnalysisSpec.model_validate(original["input"])
        assert spec_hash(spec) == original["hash"]
        primary = spec.model_copy(update={"operation": "aggregate_metric"})
        case = {"input": original["input"], "primary": capture(primary)}
        if spec.comparison_start is not None and spec.comparison_end is not None:
            comparison = spec.model_copy(
                update={
                    "operation": "aggregate_metric",
                    "start": spec.comparison_start,
                    "end": spec.comparison_end,
                    "comparison_start": None,
                    "comparison_end": None,
                }
            )
            case["comparison"] = capture(comparison)
        cases.append(case)
    output.write_text(
        "[\n"
        + ",\n".join(
            json.dumps(case, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            for case in cases
        )
        + "\n]\n",
        encoding="utf-8",
    )
    print(
        "Captured model copy cases",
        len(cases),
        "comparison copies",
        sum("comparison" in case for case in cases),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.source, args.output)
