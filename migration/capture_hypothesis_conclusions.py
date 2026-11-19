"""Capture decisions from the frozen recheck function with synthetic analysis results."""

import argparse
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from capture_hypothesis_specs import BASE

from garmin_ai import hypotheses, replay
from garmin_ai.normalize import PARSER_VERSION


class SyntheticSession:
    def __init__(self, value):
        self.row = SimpleNamespace(value=value)
        self.info = {"timezone": "UTC"}

    def get(self, *args, **kwargs):
        return self.row

    def flush(self):
        pass


def run(output):
    hypotheses.lock_writes = lambda session: None
    replay.replay_generation = lambda session: None
    intervals = [
        None,
        [],
        [-4, -1],
        [1, 4],
        [-1, 1],
        [0, 0],
        [-1, -0.0],
        [0.0, 1],
        [-1e-308, -1e-308],
        [1e-308, 1e-308],
    ]
    cases = []
    for direction, discovery_status, discovery_interval, status, interval in itertools.product(
        ["lower", "higher"],
        [None, "insufficient_evidence", "exploratory"],
        intervals,
        [None, "insufficient_evidence", "exploratory"],
        intervals,
    ):
        discovery = {
            "spec": {"method_version": "coffee-sleep-v1"},
            "status": discovery_status,
            "comparison": {"ci95": discovery_interval},
            "evidence_hash": "synthetic-discovery",
        }
        result = {
            "spec": {"method_version": "coffee-sleep-v1"},
            "status": status,
            "comparison": {"ci95": interval},
            "evidence_hash": "synthetic-validation",
            "rows": [],
        }
        spec = hypotheses.HypothesisSpec.model_validate({**BASE, "direction": direction})
        session = SyntheticSession(
            {
                "spec": spec.model_dump(mode="json"),
                "created_at": "2026-09-10T00:00:00+00:00",
                "method_version": "coffee-sleep-v1",
                "normalization_parser_version": PARSER_VERSION,
                "projection_generation": None,
                "status": "registered",
                "discovery": discovery,
                "checks": [],
            }
        )
        hypotheses.guarded_analysis = lambda *args, result=result: result
        updated = hypotheses.recheck(session, spec.id, datetime(2026, 10, 11, tzinfo=UTC))
        cases.append(
            {
                "direction": direction,
                "discovery": discovery,
                "result": result,
                "conclusion": updated["checks"][0]["conclusion"],
            }
        )
    output.write_text(
        "[\n"
        + ",\n".join(json.dumps(case, ensure_ascii=True, separators=(",", ":")) for case in cases)
        + "\n]\n",
        encoding="utf-8",
    )
    print("Captured frozen recheck decisions", len(cases), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
