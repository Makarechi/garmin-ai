"""Queued generic read answers are revalidated without storing source values."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from garmin_ai import agent, integrations, telegram
from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, IntegrationInstance, Settings
from garmin_ai.definitions import CustomEntryInput, create_custom_event, update_custom_event
from garmin_ai.i18n import translate
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import AppState, Measurement
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)


class QueryPlanner:
    def __init__(self, spec):
        self.spec = spec

    def structured(self, _instruction, prompt, schema):
        if schema is agent.Interpretation:
            return schema(intent="question", confidence=1)
        if schema is agent.SafetyScreen:
            return schema(urgent=False)
        assert schema is agent.AgentStep
        if json.loads(prompt).get("evidence"):
            return schema(answer="Synthetic query answer.", evidence_ids=[1])
        return schema(
            calls=[
                agent.ReadCall(
                    name="generic_analysis", arguments_json=json.dumps({"spec": self.spec})
                )
            ]
        )

    def close(self):
        pass


def metric(db, now):
    at = now - timedelta(minutes=2)
    version = ensure_system_metric_definitions(db)["steps_bucket"]
    db.add(
        Measurement(
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
    )
    db.flush()
    return at, version


def spec(now, operation, **extra):
    return {
        "operation": operation,
        "start": (now - timedelta(minutes=10)).isoformat(),
        "end": (now + timedelta(minutes=1)).isoformat(),
        "knowledge_cutoff": (now + timedelta(minutes=1)).isoformat(),
        **extra,
    }


def test_in_turn_generic_observations_recheck_a_concurrent_change(db, db_engine):
    now = datetime.now(UTC)
    at, _version = metric(db, now)
    db.commit()
    request = spec(now, "query_observations", metric_key="system.steps_bucket")

    class ConcurrentPlanner(QueryPlanner):
        def structured(self, instruction, prompt, schema):
            if schema is agent.AgentStep and json.loads(prompt).get("evidence"):
                with Session(db_engine) as writer, writer.begin():
                    writer.get(Measurement, (at, "steps_bucket", "synthetic")).value = 90
            return super().structured(instruction, prompt, schema)

    result = agent.answer_question(
        db,
        ConcurrentPlanner(request),
        "Synthetic question",
        Settings(_env_file=None),
        now,
        before_model=db.commit,
    )
    assert result == translate("agent.data_changed", "ru")


@pytest.mark.parametrize("operation", ["query_observations", "query_completeness"])
def test_telegram_rechecks_queued_generic_queries(db, db_engine, operation):
    now = datetime.now(UTC)
    at, version = metric(db, now)
    telegram.save_update(
        db,
        {
            "update_id": 994,
            "message": {
                "message_id": 994,
                "date": int(now.timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "Synthetic question",
            },
        },
        42,
    )
    db.commit()
    request = spec(now, operation, metric_key="system.steps_bucket", method=None)
    response = telegram.process_message(
        db_engine, QueryPlanner(request), Settings(_env_file=None, telegram_user_id=42), 994
    )
    assert "Synthetic query answer." in response
    db.expire_all()
    proof = db.get(AppState, "telegram:reply:994").value["analysis_projection"][
        "generic_metric_dependencies"
    ]
    assert proof and proof[0]["snapshots"] == []
    assert '"value": 72' not in json.dumps(proof)
    with Session(db_engine) as writer, writer.begin():
        if operation == "query_observations":
            writer.get(Measurement, (at, "steps_bucket", "synthetic")).value = 90
        else:
            other = now - timedelta(minutes=1)
            writer.add(
                Measurement(
                    ts=other,
                    metric="steps_bucket",
                    source="synthetic",
                    local_date=other.date(),
                    value=90,
                    unit="steps",
                    metric_definition_version_id=version.id,
                    quality="observed",
                    details={},
                    ingested_at=now,
                )
            )
    sent = []

    class Bot:
        async def send_message(self, **kwargs):
            sent.append(kwargs["text"])
            return SimpleNamespace(message_id=994)

    asyncio.run(telegram.deliver(Bot(), db_engine, 42, "update:994", response))
    assert len(sent) == 1 and "Synthetic query answer." not in sent[0]


def test_web_rechecks_queued_user_entries(db, db_engine, monkeypatch):
    now = datetime.now(UTC)
    draft = TrackerSetupDraft(
        key="synthetic_focus",
        name="Synthetic focus",
        locale="en",
        fields=[
            TrackerFieldDraft(key="rating", label="Rating", kind="scale", minimum=1, maximum=5)
        ],
    )
    preview = preview_tracker(db, draft)
    confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    event = create_custom_event(
        db,
        CustomEntryInput(
            definition_key="user.synthetic_focus",
            start=now - timedelta(minutes=2),
            timezone="UTC",
            values={"rating": 3},
            units={"rating": "score_1-5"},
        ),
        actor="test",
    )
    event_id = event.id
    db.commit()
    request = spec(now, "query_entries", definition_key="user.synthetic_focus")
    monkeypatch.setattr(integrations, "create_model_provider", lambda *args: QueryPlanner(request))
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
    body = {"client_message_id": str(uuid4()), "text": "Synthetic question"}
    response = api.post("/web-chat/messages", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert "Synthetic query answer." in response.json()["reply"]["text"]
    with Session(db_engine) as writer, writer.begin():
        update_custom_event(
            writer,
            event_id,
            CustomEntryInput(
                definition_key="user.synthetic_focus",
                start=now - timedelta(minutes=2),
                timezone="UTC",
                values={"rating": 4},
                units={"rating": "score_1-5"},
            ),
            revision=1,
            actor="test",
        )
    assert api.get("/web-chat/messages", headers=headers).json()["replies"] == []
    assert api.post("/web-chat/messages", json=body, headers=headers).json()["reply"] is None
