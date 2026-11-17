"""Capture reference attempts, fallback decisions and gate writes from pinned Python."""

import argparse
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from check_model_gate_outcomes import KEY, NOW, PRIMARY, fixture_gate
from sqlalchemy.orm import Session

from garmin_ai.llm import (
    ProviderAuthError,
    ProviderConsentRequired,
    ProviderCooldown,
    ProviderFallbackDeadline,
    ProviderModelUnavailable,
    ProviderOutputInvalid,
    ProviderOutputTruncated,
    ProviderRateLimited,
    ProviderRequestInvalid,
    ProviderUnavailable,
)
from garmin_ai.models import AppState, Job
from garmin_ai.provider_gate import ACTIVE_JOB

MODELS = ["first", "second"]
CASES = {
    "primary_ok": {"steps": ["ok"]},
    "initial_auth": {"steps": ["auth"]},
    "quota_then_success": {"steps": ["quota", "ok"]},
    "both_temporary": {"steps": ["quota", "upstream"]},
    "tie_uses_attempt_order": {"steps": ["quota", "earlier_equal"]},
    "provider_paused": {"blocked": 19, "steps": []},
    "all_models_cooling": {"cooldowns": {"first": 18, "second": 30}, "steps": []},
    "cooling_primary_then_fallback_auth": {"cooldowns": {"first": 18}, "steps": ["auth"]},
    "temporary_then_invalid_request": {"steps": ["quota", "request"]},
    "initial_cooling_then_invalid_request": {"cooldowns": {"first": 18}, "steps": ["request"]},
    "invalid_output_then_invalid_request": {"steps": ["invalid", "request"]},
    "truncated_then_invalid_request": {"steps": ["incomplete", "request"]},
    "invalid_precedes_quota": {"steps": ["invalid", "quota"]},
    "all_invalid": {"steps": ["invalid", "incomplete"]},
    "checkpoint": {"steps": ["invalid", "deadline"], "checkpoint": True},
    "checkpoint_write_failed": {"steps": ["invalid", "deadline"], "checkpoint": False},
    "no_job_checkpoint": {"steps": ["invalid", "deadline"], "job": False},
    "resume_saved_candidate": {"steps": ["ok"], "excluded": ["first"]},
    "resume_all_invalid": {"steps": [], "excluded": MODELS},
    "resume_then_invalid_request": {"steps": ["request"], "excluded": ["first"]},
    "consent_revoked_between_attempts": {"steps": ["quota", "denied"]},
    "single_plugin_quota": {"models": [None], "steps": ["quota"]},
    "single_plugin_deadline": {"models": [None], "steps": ["deadline"]},
    "invalid_with_other_model_cooling": {"cooldowns": {"first": 18}, "steps": ["invalid"]},
}


def time_at(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def fixture(engine, index, name, case):
    models = case.get("models", MODELS)
    excluded = case.get("excluded", [])
    gate = fixture_gate(engine, PRIMARY, KEY, False)
    at = case.get("cooldowns", {})
    stored = {
        "configuration": gate.configuration,
        "model_cooldowns": {key: time_at(delay) for key, delay in at.items()},
    }
    if "blocked" in case:
        stored.update(reason="quota", blocked_until=time_at(case["blocked"]))
    job_id = UUID(int=index) if case.get("job", True) else None
    kwargs = {"prompt": "synthetic fallback fixture"}
    fingerprint = hashlib.sha256(repr((models, ["diary"], kwargs)).encode()).hexdigest()
    with Session(engine) as session, session.begin():
        session.merge(AppState(key=gate.state_key, value=stored))
        if job_id is not None:
            payload = (
                {
                    "provider_resume": {
                        "identity_version": 2,
                        "configuration": gate.resume_configuration,
                        "models": models,
                        "invalid_models": excluded,
                        "request_fingerprint": fingerprint,
                    }
                }
                if excluded
                else {}
            )
            session.add(
                Job(
                    id=job_id,
                    kind="agent_insights",
                    payload=payload,
                    dedup_key=f"synthetic-fallback:{index}",
                    run_at=NOW,
                    status="running",
                    attempts=1,
                )
            )
    attempted = []
    writes = []
    inputs = iter(case["steps"])
    failure = {
        "auth": ProviderAuthError,
        "quota": ProviderRateLimited,
        "upstream": ProviderUnavailable,
        "earlier_equal": ProviderModelUnavailable,
        "invalid": ProviderOutputInvalid,
        "incomplete": ProviderOutputTruncated,
        "request": ProviderRequestInvalid,
        "deadline": ProviderFallbackDeadline,
        "denied": ProviderConsentRequired,
    }

    def request(model=None, **_kwargs):
        attempted.append(model)
        kind = next(inputs)
        if kind == "ok":
            return "synthetic result"
        error = failure[kind]("synthetic error")
        if kind == "earlier_equal":
            error.retry_seconds = 120
        raise error

    def record_gate(reason, until, cooldowns=None, *, scheduler_pause_until=None):
        writes.append(
            [
                "gate",
                reason,
                until.isoformat() if until else None,
                scheduler_pause_until.isoformat() if scheduler_pause_until else None,
                dict(cooldowns or {}),
            ]
        )

    def checkpoint(_id, ordered, _digest, invalid):
        writes.append(["checkpoint", [model for model in ordered if model in invalid]])
        return case.get("checkpoint", True)

    gate.record_outcome = record_gate
    gate.record_resume = checkpoint
    gate.clear_resume = lambda *_args: writes.append(["clear"])
    token = ACTIVE_JOB.set(job_id)
    try:
        result = gate.call(
            request,
            model_categories={"diary"},
            models=None if models == [None] else models,
            **kwargs,
        )
        outcome = ["ok", result]
    except ProviderCooldown as exc:
        outcome = ["cooldown", exc.reason, exc.retry_seconds]
    except Exception as exc:
        classes = {
            ProviderAuthError: "auth",
            ProviderRateLimited: "quota",
            ProviderUnavailable: "upstream",
            ProviderModelUnavailable: "missing",
            ProviderOutputInvalid: "invalid",
            ProviderOutputTruncated: "incomplete",
            ProviderRequestInvalid: "request",
            ProviderFallbackDeadline: "deadline",
            ProviderConsentRequired: "denied",
        }
        code = classes[type(exc)]
        outcome = [code]
        if code in {"auth", "quota", "upstream", "missing"}:
            outcome.append(exc.retry_seconds)
    finally:
        ACTIVE_JOB.reset(token)
    assert next(inputs, None) is None, name
    return {
        "name": name,
        **case,
        "models": models,
        "excluded": excluded,
        "attempted": attempted,
        "writes": writes,
        "outcome": outcome,
    }


def record(path):
    engine = isolated_engine()
    try:
        cases = [
            fixture(engine, index, name, case)
            for index, (name, case) in enumerate(CASES.items(), start=1)
        ]
        path.write_text(json.dumps(cases), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    record(parser.parse_args().output)
