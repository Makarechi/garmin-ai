"""Record the pinned Python fallback resume writes and cleanup on a disposable store."""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from check_model_gate_outcomes import KEY, NOW, OPENAI, PRIMARY, SECONDARY, fixture_gate
from sqlalchemy.orm import Session

from garmin_ai.models import Job

CASES = [
    ("primary", PRIMARY, KEY, ["fixture-model", "fallback"], ["fixture-model"]),
    ("scoped", SECONDARY, KEY, ["fixture-model", "fallback", "third"], ["third", "fixture-model"]),
    ("plugin", OPENAI, "synthetic-openai", [None], [None]),
    ("rotated", PRIMARY, KEY + "-rotated", ["fixture-model", "fallback"], ["fallback"]),
]


def payload(engine, identity):
    with Session(engine) as session:
        return session.get(Job, identity).payload


def record(path):
    engine = isolated_engine()
    try:
        cases = []
        for index, (name, instance, key, models, invalid) in enumerate(CASES, start=1):
            identity = UUID(int=index)
            gate = fixture_gate(engine, instance, key, False)
            original = {"synthetic_job_field": {"version": 3}}
            with Session(engine) as session, session.begin():
                session.add(
                    Job(
                        id=identity,
                        kind="agent_insights",
                        payload=original,
                        dedup_key=f"synthetic-resume:{name}",
                        run_at=NOW,
                        status="running",
                        attempts=1,
                    )
                )
            fingerprint = hashlib.sha256(
                repr(
                    (models, ["diary"], {"prompt": "Synthetic user input ☕", "do_store": False})
                ).encode()
            ).hexdigest()
            assert gate.record_resume(identity, models, fingerprint, set(invalid))
            saved = payload(engine, identity)
            assert "prompt" not in json.dumps(saved)
            gate.clear_resume(identity, "b" * 64)
            assert payload(engine, identity) == saved
            gate.clear_resume(identity, fingerprint)
            cleared = payload(engine, identity)
            assert cleared == original
            cases.append(
                {
                    "name": name,
                    "instance": instance,
                    "key": key,
                    "models": models,
                    "invalid": invalid,
                    "fingerprint": fingerprint,
                    "saved": saved,
                    "cleared": cleared,
                }
            )
        path.write_text(json.dumps(cases), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    record(parser.parse_args().output)
