"""Compare native provider cooldown writes and quota jobs against pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from capture_reference import isolated_engine
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from garmin_ai import provider_gate
from garmin_ai.models import AppState, Job

NOW = datetime(2026, 3, 12, 12, tzinfo=UTC)
PRIMARY = "model:gemini:primary"
SECONDARY = "model:gemini:secondary"
OPENAI = "model:openai:primary"
KEY = "synthetic-gemini"


def fixture_gate(engine, instance, key, notify):
    plugin = SimpleNamespace(
        id=instance,
        provider=instance.split(":")[1],
        secret_refs={"api_key": "GA_SYNTHETIC_FIXTURE_KEY"},
    )
    settings = SimpleNamespace(
        gemini_model="fixture-model",
        gemini_fallback_enabled=True,
        gemini_fallback_models=["fallback"],
        llm_consent=SimpleNamespace(fallback_models=["fallback"]),
        gemini_api_key=SecretStr(key),
        telegram_user_id=1 if notify else None,
        telegram_bot_token=SecretStr("synthetic-telegram" if notify else ""),
    )
    with patch.dict(os.environ, {"GA_SYNTHETIC_FIXTURE_KEY": key}):
        return provider_gate.ProviderGate(engine, settings, clock=lambda: NOW, instance=plugin)


def fixtures(engine):
    primary = fixture_gate(engine, PRIMARY, KEY, False)
    legacy = {
        "configuration": primary.configuration,
        "scheduler_pause_until": "2026-03-12T14:07:00+02:00",
        "reason": "ready",
        "blocked_until": None,
    }
    expired = {**legacy, "scheduler_pause_until": NOW.isoformat()}
    explicit = {**legacy, "instance_id": PRIMARY}
    later = NOW + timedelta(minutes=8)
    earlier = NOW + timedelta(minutes=5)
    cases = [
        ("new_primary", PRIMARY, KEY, "ready", None, None, None, False),
        ("quota_with_notice", PRIMARY, KEY, "quota", earlier, None, None, True),
        ("quota_without_notice", PRIMARY, KEY, "quota", earlier, None, None, False),
        ("preserve_later_legacy_pause", PRIMARY, KEY, "ready", None, earlier, legacy, False),
        ("later_scheduler_pause", PRIMARY, KEY, "ready", None, later, legacy, False),
        ("expired_legacy_pause", PRIMARY, KEY, "model", earlier, None, expired, False),
        ("preserve_explicit_pause", PRIMARY, KEY, "auth", earlier, None, explicit, False),
        ("credential_change", PRIMARY, KEY + "-rotated", "ready", None, None, legacy, False),
        ("legacy_secondary", SECONDARY, KEY, "model_cooldown", earlier, None, legacy, True),
        ("explicit_primary_ignores_legacy", SECONDARY, KEY, "ready", None, None, explicit, False),
        (
            "plugin_quota_does_not_notify",
            OPENAI,
            "synthetic-openai",
            "quota",
            earlier,
            None,
            None,
            True,
        ),
    ]
    results = []
    for name, instance, key, reason, blocked, scheduler, existing, notify in cases:
        gate = fixture_gate(engine, instance, key, notify)
        with Session(engine) as session, session.begin():
            session.execute(
                delete(AppState).where(AppState.key.in_([provider_gate.KEY, gate.state_key]))
            )
            session.execute(delete(Job).where(Job.kind == "telegram_provider_notice"))
            if existing is not None:
                session.add(AppState(key=provider_gate.KEY, value=existing))
        deadlines = {"fallback": (NOW + timedelta(seconds=125)).isoformat()}
        for _ in range(2):
            gate.record(reason, blocked, deadlines, scheduler_pause_until=scheduler)
        with Session(engine) as session:
            state = session.get(AppState, gate.state_key).value
            notice = session.scalar(
                select(Job.dedup_key).where(Job.kind == "telegram_provider_notice")
            )
        results.append(
            {
                "name": name,
                "instance": instance,
                "key": key,
                "now": NOW.isoformat(),
                "reason": reason,
                "blocked_until": blocked.isoformat() if blocked else None,
                "scheduler_until": scheduler.isoformat() if scheduler else None,
                "model_cooldowns": deadlines,
                "existing_primary": existing,
                "expected": state,
                "expected_notice": notice,
                "notify": notify,
            }
        )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        tests = fixtures(engine)
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "model-gate-outcomes.json"
            destination.write_text(json.dumps(tests), encoding="utf-8")
            subprocess.run(
                [args.binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_MODEL_GATE_OUTCOMES": str(destination)},
            )
        print(f"Matched {len(tests)} saved provider gate outcomes and quota decisions")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
