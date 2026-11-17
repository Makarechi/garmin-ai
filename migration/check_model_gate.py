"""Compare synthetic model gate identities and scheduler decisions against pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from capture_reference import isolated_engine
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.orm import Session

from garmin_ai import provider_gate
from garmin_ai.models import AppState

NOW = datetime(2026, 3, 12, 12, tzinfo=UTC)
UNTIL = "2026-03-12T12:01:00+00:00"
OTHER_OFFSET = "2026-03-12T14:02:00+02:00"
OTHER_UTC = "2026-03-12T12:02:00+00:00"
PRIMARY = "model:gemini:primary"


def identity(engine, instance, provider, model, fallback, models, approved, secrets):
    settings = SimpleNamespace(
        gemini_model=model,
        gemini_fallback_enabled=fallback,
        gemini_fallback_models=models,
        llm_consent=SimpleNamespace(fallback_models=approved),
        gemini_api_key=SecretStr(secrets.get("api_key") or ""),
        telegram_user_id=None,
    )
    refs = {name: f"GA_SYNTHETIC_MODEL_FIXTURE_{n}" for n, name in enumerate(secrets)}
    plugin = SimpleNamespace(id=instance, provider=provider, secret_refs=refs)
    environment = {refs[name]: value for name, value in secrets.items() if value is not None}
    with patch.dict(os.environ, environment):
        gate = provider_gate.ProviderGate(engine, settings, clock=lambda: NOW, instance=plugin)
    data = {
        "instance": instance,
        "provider": provider,
        "model": model,
        "fallback_enabled": fallback,
        "fallback_models": models,
        "consent_models": approved,
        "secrets": secrets,
        "expected": [gate.state_key, gate.lock_key, gate.configuration, gate.resume_configuration],
    }
    return (data, settings, plugin, environment)


def fixtures(engine):
    gemini = ("gemini", "fixture-model", True, ["fallback-A", "fallback-B"], ["fallback-A"])
    plugin = ("fixture-model", False, [], [])
    identities = [
        identity(engine, PRIMARY, *gemini, {"api_key": "synthetic-gemini"}),
        identity(engine, "model:gemini:secondary", *gemini, {"api_key": "synthetic-gemini"}),
        identity(engine, PRIMARY, *gemini, {"api_key": "synthetic-gemini-rotated"}),
        identity(
            engine,
            "model:openai:primary",
            "openai",
            *plugin,
            {"z": None, "api_key": "synthetic-openai", "a": "", "🧪": "Данные"},
        ),
        identity(
            engine,
            "model:openai:primary",
            "openai",
            *plugin,
            {"a": "", "z": None, "🧪": "Данные", "api_key": "synthetic-openai-rotated"},
        ),
        identity(engine, "model:anthropic:primary", "anthropic", *plugin, {"api_key": None}),
        identity(
            engine,
            "model:gemini:unicode",
            "gemini",
            "модель\n🧪",
            True,
            ["𐐷", "Ω"],
            ["Ω"],
            {"api_key": "synthetic-У\x7f"},
        ),
        identity(
            engine,
            PRIMARY,
            *gemini[:3],
            list(reversed(gemini[3])),
            gemini[4],
            {"api_key": "synthetic-gemini"},
        ),
    ]

    def value(index, **fields):
        return {"configuration": identities[index][0]["expected"][2], **fields}

    active = value(0, blocked_until=UNTIL, reason="quota")
    scheduled = value(0, scheduler_pause_until=OTHER_OFFSET)
    with_models = value(
        0,
        blocked_until="broken",
        scheduler_pause_until=NOW.isoformat(),
        model_cooldowns={
            "fixture-model": OTHER_OFFSET,
            "fallback-A": NOW.isoformat(),
            "naive": "2026-03-12T12:20:00",
            "date-only": "2026-03-12",
            "number": 123,
            "garbled": "broken",
        },
    )
    cases = [
        ("missing", 0, None, None, None, None, {}),
        ("primary_global", 0, active, None, ["quota", UNTIL], None, {}),
        ("secondary_inherits", 1, active, None, ["quota", UNTIL], None, {}),
        (
            "secondary_ignores_explicit_primary",
            1,
            {**active, "instance_id": PRIMARY},
            None,
            None,
            None,
            {},
        ),
        (
            "secondary_ignores_null_primary_tag",
            1,
            {**active, "instance_id": None},
            None,
            None,
            None,
            {},
        ),
        ("secondary_prefers_scoped", 1, active, scheduled, None, OTHER_UTC, {}),
        (
            "secondary_wrong_scoped_excludes_legacy",
            1,
            active,
            value(2, blocked_until=UNTIL),
            None,
            None,
            {},
        ),
        ("rotated_gemini", 2, active, None, None, None, {}),
        ("changed_fallback_order", 7, active, None, None, None, {}),
        ("active_model_only", 0, with_models, None, None, None, {"fixture-model": OTHER_UTC}),
        ("scheduler_only", 0, scheduled, None, None, OTHER_UTC, {}),
        ("plugin_isolated", 3, active, None, None, None, {}),
        (
            "plugin_retry",
            3,
            active,
            value(3, blocked_until=UNTIL, reason="auth"),
            ["auth", UNTIL],
            None,
            {},
        ),
        ("plugin_rotated", 4, active, None, None, None, {}),
        ("plugin_stale_configuration", 4, active, value(3, blocked_until=UNTIL), None, None, {}),
        ("invalid_document", 5, active, "unrecognized state", None, None, {}),
        (
            "unknown_reason",
            6,
            None,
            value(6, blocked_until=UNTIL, reason={"private": "fixture"}),
            ["unavailable", UNTIL],
            None,
            {},
        ),
    ]
    reads = []
    for name, index, legacy, scoped, blocked, scheduler, models in cases:
        data, settings, instance, environment = identities[index]
        state_key = data["expected"][0]
        with Session(engine) as session, session.begin():
            session.execute(
                delete(AppState).where(AppState.key.in_([provider_gate.KEY, state_key]))
            )
            for key, document in [(provider_gate.KEY, legacy), (state_key, scoped)]:
                if document is not None:
                    session.add(AppState(key=key, value=document))
            session.flush()
            with (
                patch.dict(os.environ, environment),
                patch("garmin_ai.integrations.configured_model_instance", return_value=instance),
            ):
                paused = provider_gate.paused(session, NOW, settings=settings)
        assert paused == bool(blocked or scheduler), name
        reads.append(
            {
                "name": name,
                "identity": index,
                "legacy": legacy,
                "scoped": scoped,
                "scheduler_paused": paused,
                "blocked": blocked,
                "scheduler_until": scheduler,
                "model_cooldowns": models,
            }
        )
    return {"now": NOW.isoformat(), "identities": [row[0] for row in identities], "reads": reads}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        fixture = fixtures(engine)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-model-gate.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            subprocess.run(
                [args.binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_MODEL_GATE": str(path)},
            )
        print(
            f"Matched {len(fixture['identities'])} identities and {len(fixture['reads'])} scheduler cases"
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
