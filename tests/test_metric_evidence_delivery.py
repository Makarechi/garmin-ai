"""Queued synthetic answers must not expose changed metric evidence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai import agent, integrations, telegram, web_chat
from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, IntegrationInstance, Settings
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import AppState, Measurement, OutboxMessage
from garmin_ai.sleep_timing import timing_dependencies_current


def synthetic_metric(db):
    now = datetime.now(UTC)
    at = now - timedelta(minutes=2)
    version = ensure_system_metric_definitions(db)["steps_bucket"]
    row = Measurement(
        ts=at,
        metric="steps_bucket",
        source="synthetic",
        local_date=at.date(),
        value=72,
        unit="steps",
        metric_definition_version_id=version.id,
        quality="observed",
        details={},
        ingested_at=now - timedelta(seconds=1),
    )
    db.add(row)
    db.flush()
    return now, at


class Planner:
    def __init__(self, now):
        self.now = now

    def structured(self, _instruction, prompt, schema):
        if schema is agent.Interpretation:
            return schema(intent="question", confidence=1)
        if schema is agent.SafetyScreen:
            return schema(urgent=False)
        assert schema is agent.AgentStep
        evidence = json.loads(prompt).get("evidence", [])
        if evidence:
            assert evidence[0]["result"]["value"] == 72
            return schema(answer="Synthetic metric answer.", evidence_ids=[1])
        spec = {
            "operation": "aggregate_metric",
            "metric_key": "system.steps_bucket",
            "start": (self.now - timedelta(minutes=10)).isoformat(),
            "end": (self.now + timedelta(minutes=1)).isoformat(),
            "knowledge_cutoff": (self.now + timedelta(minutes=1)).isoformat(),
            "method": "sum",
        }
        return schema(
            calls=[
                agent.ReadCall(name="generic_analysis", arguments_json=json.dumps({"spec": spec}))
            ]
        )

    def close(self):
        pass


def change_metric(engine, at):
    with Session(engine) as session, session.begin():
        row = session.get(Measurement, (at, "steps_bucket", "synthetic"))
        row.value = 90


@pytest.mark.parametrize("proof", [{}, {"request_time": "invalid"}, None, []])
def test_invalid_metric_proof_fails_closed(db, proof):
    assert not timing_dependencies_current(
        db, {"generic_metric_dependencies": [proof]}, for_model=True
    )


def test_delivery_proof_contains_only_cited_metric_dependencies(db):
    now, _at = synthetic_metric(db)

    class TwoMetrics(Planner):
        def structured(self, instruction, prompt, schema):
            step = super().structured(instruction, prompt, schema)
            if schema is agent.AgentStep and step.calls:
                first_spec = json.loads(step.calls[0].arguments_json)["spec"]
                first_spec["metric_key"] = "system.stress_score"
                first_spec["method"] = "max"
                step.calls.append(
                    agent.ReadCall(
                        name="generic_analysis", arguments_json=json.dumps({"spec": first_spec})
                    )
                )
            return step

    response = agent.answer_question(
        db, TwoMetrics(now), "Synthetic question", Settings(_env_file=None), now
    )
    assert "Synthetic metric answer." in response
    proof = db.info["analysis_projection"]["generic_metric_dependencies"]
    assert len(proof) == 1
    assert proof[0]["arguments"]["spec"]["metric_key"] == "system.steps_bucket"


@pytest.mark.parametrize("change", ["none", "metric", "consent"])
def test_telegram_rechecks_metric_before_sending(db, db_engine, change):
    now, at = synthetic_metric(db)
    telegram.save_update(
        db,
        {
            "update_id": 997,
            "message": {
                "message_id": 997,
                "date": int(now.timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "Synthetic question",
            },
        },
        42,
    )
    db.commit()
    response = telegram.process_message(
        db_engine, Planner(now), Settings(_env_file=None, telegram_user_id=42), 997
    )
    assert "Synthetic metric answer." in response
    db.expire_all()
    proof = db.get(AppState, "telegram:reply:997").value["analysis_projection"]
    assert proof["generic_metric_dependencies"]
    assert '"value": 72' not in json.dumps(proof)
    if change == "metric":
        change_metric(db_engine, at)
    elif change == "consent":
        with Session(db_engine) as session, session.begin():
            session.add(
                AppState(
                    key="preferences:onboarding",
                    value={
                        "model_categories": ["diary"],
                        "channel": {"channel": "telegram", "instance_id": "primary"},
                    },
                )
            )
    sent = []

    class Bot:
        async def send_message(self, **kwargs):
            sent.append(kwargs["text"])
            return SimpleNamespace(message_id=997)

    asyncio.run(telegram.deliver(Bot(), db_engine, 42, "update:997", response))
    assert len(sent) == 1
    assert ("Synthetic metric answer." in sent[0]) is (change == "none")


@pytest.mark.parametrize("stage", ["initial", "history"])
def test_web_rechecks_metric_at_each_exposure(db, db_engine, monkeypatch, stage):
    now, at = synthetic_metric(db)
    db.commit()
    monkeypatch.setattr(integrations, "create_model_provider", lambda *args: Planner(now))
    key = "synthetic-owner-" + "a" * 32
    api = TestClient(
        create_app(
            Settings(
                _env_file=None,
                api_tokens=[ApiToken(key=key, scopes={"admin"})],
                integrations=[
                    IntegrationInstance(id="model:gemini:offline", kind="model", provider="gemini")
                ],
            ),
            db_engine,
        )
    )
    headers = {"Authorization": "Bearer " + key}
    original = web_chat._fenced_response
    changed = []

    def fenced(engine, body, intents, **kwargs):
        if stage == "initial" and "reply" in body and not changed:
            change_metric(db_engine, at)
            changed.append(True)
        return original(engine, body, intents, **kwargs)

    monkeypatch.setattr(web_chat, "_fenced_response", fenced)
    body = {"client_message_id": str(uuid4()), "text": "Synthetic question"}
    response = api.post("/web-chat/messages", json=body, headers=headers)
    assert response.status_code == 200, response.text
    if stage == "initial":
        assert response.json()["reply"] is None
    else:
        assert "Synthetic metric answer." in response.json()["reply"]["text"]
        change_metric(db_engine, at)
    assert api.get("/web-chat/messages", headers=headers).json()["replies"] == []
    assert api.post("/web-chat/messages", json=body, headers=headers).json()["reply"] is None
    db.expire_all()
    outbox = db.scalar(select(OutboxMessage))
    assert (
        api.post(f"/web-chat/messages/{outbox.id}/read", json={}, headers=headers).status_code
        == 409
    )
