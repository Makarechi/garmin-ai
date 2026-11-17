"""Capture synthetic fallback digests from the pinned Gemini provider gate."""

import argparse
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from capture_reference import isolated_engine
from check_model_gate_outcomes import KEY, NOW, PRIMARY, fixture_gate
from generate_model_schemas import APPROVED
from sqlalchemy.orm import Session

from garmin_ai.llm import GeminiProvider, ProviderCooldown, ProviderFallbackDeadline
from garmin_ai.models import Job
from garmin_ai.provider_gate import ACTIVE_JOB

MODELS = ["synthetic-first", "synthetic-last"]
TEXT = "Synthetic 'single' and \"double\" \\slash\n\t\r\0\x85\u00a0 cafe\u0301 🧑\u200d🔬 \ue000 \U0010ffff"
AUDIO = bytes([0, 127, 128, 255, 42])


def record(path):
    engine = isolated_engine()
    try:
        cases = []
        for index, (name, level, categories) in enumerate(
            [
                ("agent-step", "low", ["health", "diary"]),
                ("interpretation", "", ["diary"]),
                ("safety-screen", "low", ["health"]),
                ("tracker-extraction", "", ["diary"]),
                ("model-probe", "", ["health", "diary"]),
                ("transcript", "", ["audio"]),
                ("transcript", "low", ["audio"]),
            ],
            start=1,
        ):
            job = UUID(int=index)
            with Session(engine) as session, session.begin():
                session.add(
                    Job(
                        id=job,
                        kind="agent_insights",
                        payload={},
                        dedup_key=f"synthetic-digest:{index}",
                        run_at=NOW,
                        status="running",
                        attempts=1,
                    )
                )
            provider = object.__new__(GeminiProvider)
            provider.model = MODELS[0]
            provider._authorize = lambda *_args, **_kwargs: None
            provider._model_chain = lambda _categories: MODELS
            provider.request_gate = fixture_gate(engine, PRIMARY, KEY, False)
            provider.generation_config = {"thinking_level": level} if level else {}
            attempted = []

            def request(model, *, attempted=attempted, **_kwargs):
                attempted.append(model)
                if model == MODELS[-1]:
                    raise ProviderFallbackDeadline("synthetic fallback deadline")
                return SimpleNamespace(status="completed", output_text="{invalid}")

            provider._request = request
            token = ACTIVE_JOB.set(job)
            try:
                if name == "transcript":
                    provider.transcribe(AUDIO, "audio/ogg")
                else:
                    provider.structured_for_categories(
                        "Don't alter the original text", TEXT, APPROVED[name], set(categories)
                    )
            except ProviderCooldown as exc:
                assert (exc.reason, exc.retry_seconds) == ("model_cooldown", 1)
            else:
                raise AssertionError("Synthetic gate did not record a fallback checkpoint")
            finally:
                ACTIVE_JOB.reset(token)
            assert attempted == MODELS
            with Session(engine) as session:
                saved = session.get(Job, job).payload["provider_resume"]
            assert saved["models"] == MODELS and saved["invalid_models"] == MODELS[:1]
            cases.append(
                {
                    "name": name,
                    "level": level,
                    "categories": categories,
                    "models": MODELS,
                    "digest": saved["request_fingerprint"],
                }
            )
        path.write_text(
            json.dumps(
                {"text": TEXT, "audio": base64.b64encode(AUDIO).decode(), "cases": cases},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    record(parser.parse_args().output)
