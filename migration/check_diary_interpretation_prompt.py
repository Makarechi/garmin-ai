"""Capture bounded diary prompts from the pinned Python interpreter."""

import argparse
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from garmin_ai.agent import interpret
from garmin_ai.config import Settings


class PromptCaptured(Exception):
    pass


class Recorder:
    def structured(self, instruction, prompt, schema):
        self.prompt = json.loads(prompt)
        self.instruction = instruction
        assert schema.__name__ == "Interpretation"
        raise PromptCaptured


def cases():
    now = datetime.fromisoformat("2026-10-25T01:31:00.123400+00:00")
    zone = "Europe/Bratislava"
    context = {
        "recent_analysis_question": None,
        "recent_events": [
            {
                "id": str(UUID(int=index + 1)),
                "kind": "note",
                "status": "confirmed",
                "end": now.isoformat(),
                "payload": {"text": "🧪" * 301},
                "original_text": "synthetic-private-source-text",
            }
            for index in range(20)
        ],
        "history_truncated": False,
        "pending_clarification": {"prompt": "Synthetic clarification"},
        "recent_questions": [{"id": str(UUID(int=51)), "text": "Synthetic question"}],
    }
    context["recent_events"].extend(
        [
            {
                "id": str(UUID(int=41)),
                "kind": "migraine",
                "status": "confirmed",
                "end": None,
                "original_text": "synthetic-private-source-text",
            },
            {"id": str(UUID(int=42)), "kind": "illness", "status": "confirmed", "end": None},
        ]
    )
    request = {
        "now": now.isoformat(),
        "timezone": zone,
        "text": "Внеси синтетическую запись",
        "context": context,
    }
    yield "local_time_and_open_context", request
    overflow = deepcopy(request)
    overflow["context"]["pending_clarification"]["prompt"] = "x" * 30_000
    yield "oversized_context", overflow


def capture(output):
    result = []
    for name, request in cases():
        recorder = Recorder()
        settings = Settings(_env_file=None, timezone=request["timezone"])
        with (
            patch("garmin_ai.accounts.effective_owner_settings", return_value=settings),
            patch("garmin_ai.agent.context_for", return_value=deepcopy(request["context"])),
        ):
            try:
                interpret(
                    SimpleNamespace(info={}),
                    recorder,
                    request["text"],
                    settings,
                    datetime.fromisoformat(request["now"]),
                )
            except PromptCaptured:
                pass
            else:
                raise AssertionError("The synthetic interpretation did not reach its recorder")
        assert recorder.prompt["now"] == "2026-10-25T02:31:00.123400+01:00"
        assert "synthetic-private-source-text" not in json.dumps(recorder.prompt)
        result.append(
            {
                "name": name,
                "request": request,
                "expected": recorder.prompt,
                "instruction": recorder.instruction,
            }
        )
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
