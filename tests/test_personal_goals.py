import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from garmin_ai.agent import AgentStep, ReadCall, answer_question
from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.events import Conflict
from garmin_ai.models import AppState, Job
from garmin_ai.personal_goals import KEY, GoalSelection, preferences, select_goals
from garmin_ai.telegram import process_message, save_update

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def test_selection_is_explicit_reversible_and_revision_checked(db):
    assert preferences(db) == {"configured": False, "revision": 0, "goals": [], "updated_at": None}
    first = select_goals(db, GoalSelection(revision=0, goals=["sleep", "wellbeing"]), NOW)
    assert first["configured"] and "running" not in first["goals"]
    with pytest.raises(Conflict):
        select_goals(db, GoalSelection(revision=0, goals=["running"]), NOW)
    assert select_goals(db, GoalSelection(revision=1, goals=["wellbeing", "sleep"]), NOW) == first
    empty = select_goals(db, GoalSelection(revision=1, goals=[]), NOW)
    assert empty["configured"] and empty["goals"] == [] and empty["revision"] == 2
    db.expire_all()
    assert preferences(db) == empty
    assert db.get(AppState, KEY).value["history"][-1] == first


@pytest.mark.parametrize("goals", [["sleep", "sleep"], ["invented"]])
def test_invalid_goal_selection_is_rejected(goals):
    with pytest.raises(ValidationError):
        GoalSelection(revision=0, goals=goals)


def test_goals_api_enforces_scopes_and_conflicting_revisions(db, db_engine):
    readonly, write = "synthetic-read-key-" + "x" * 32, "synthetic-write-key-" + "x" * 32
    settings = Settings(
        api_tokens=[
            ApiToken(key=readonly, scopes={"read:diary"}),
            ApiToken(key=write, scopes={"read:diary", "write:diary"}),
        ]
    )
    client = TestClient(create_app(settings, db_engine))
    read_headers = {"Authorization": "Bearer " + readonly}
    write_headers = {"Authorization": "Bearer " + write}
    assert client.get("/preferences/goals").status_code == 401
    assert not client.get("/preferences/goals", headers=read_headers).json()["configured"]
    body = {"revision": 0, "goals": ["sleep"]}
    assert client.put("/preferences/goals", headers=read_headers, json=body).status_code == 403
    assert client.put("/preferences/goals", headers=write_headers, json=body).json()["goals"] == [
        "sleep"
    ]
    assert client.put("/preferences/goals", headers=write_headers, json=body).status_code == 409


def test_offline_goals_command_bypasses_waiting_analysis_and_survives_retry(db, db_engine):
    for identity, text in [(1, "synthetic unanswered question"), (2, "/goals сон самочувствие")]:
        save_update(
            db,
            {
                "update_id": identity,
                "message": {
                    "message_id": identity,
                    "date": NOW.isoformat(),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": text,
                },
            },
            42,
        )
    db.commit()
    settings = Settings(telegram_user_id=42, timezone="UTC")
    result = process_message(db_engine, None, settings, 2)
    assert "Спортивная цель выключена" in result
    assert process_message(db_engine, None, settings, 2) == result
    db.expire_all()
    assert preferences(db)["goals"] == ["sleep", "wellbeing"]
    assert db.scalar(select(Job).where(Job.dedup_key == "telegram:2")).kind == "telegram_control"


def test_analysis_receives_only_explicit_goal_preferences(db):
    select_goals(db, GoalSelection(revision=0, goals=["sleep"]), NOW)

    class Provider:
        calls = 0

        def structured(self, instruction, prompt, schema):
            value = json.loads(prompt)
            assert value["personal_goals"]["goals"] == ["sleep"]
            assert value["personal_goals"]["configured"]
            self.calls += 1
            if self.calls == 1:
                return AgentStep(
                    calls=[
                        ReadCall(
                            name="events",
                            arguments_json=json.dumps(
                                {
                                    "start": NOW.isoformat(),
                                    "end": (NOW + timedelta(hours=1)).isoformat(),
                                }
                            ),
                        )
                    ]
                )
            return AgentStep(answer="Synthetic answer", evidence_ids=[1])

    assert "Synthetic answer" in answer_question(
        db, Provider(), "synthetic", Settings(timezone="UTC"), NOW
    )
