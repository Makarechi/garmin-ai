"""Export only the five application-owned provider response contracts."""

import argparse
import json
from pathlib import Path

from garmin_ai.agent import AgentStep, Interpretation, SafetyScreen
from garmin_ai.extension_tck import ModelProbe
from garmin_ai.model_schema_policy import trusted_model_schema
from garmin_ai.natural_language import TrackerExtraction

APPROVED = {
    "agent-step": AgentStep,
    "interpretation": Interpretation,
    "safety-screen": SafetyScreen,
    "tracker-extraction": TrackerExtraction,
    "model-probe": ModelProbe,
}


def export(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for key, model in APPROVED.items():
        assert trusted_model_schema(model)
        (directory / f"{key}.json").write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    export(parser.parse_args().directory)
