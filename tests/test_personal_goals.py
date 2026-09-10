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


@pytest.mark.parametrize("same_time", [False, True])
def test_late_goal_command_cannot_restore_an_older_selection(db, same_time):
    from garmin_ai.personal_goals import telegram_goals

    telegram_goals(db, "/goals нет", NOW, sent_at=NOW, update_id=20)
    telegram_goals(
        db,
        "/goals бег",
        NOW,
        sent_at=NOW if same_time else NOW - timedelta(seconds=1),
        update_id=19,
    )
    assert preferences(db)["goals"] == []
    telegram_goals(db, "/goals нет", NOW, sent_at=NOW + timedelta(seconds=2), update_id=22)
    telegram_goals(db, "/goals бег", NOW, sent_at=NOW + timedelta(seconds=1), update_id=21)
    assert preferences(db)["goals"] == []


def test_changed_goals_fence_an_inflight_model_answer(db):
    select_goals(db, GoalSelection(revision=0, goals=["running"]), NOW)

    class Provider:
        def structured(self, instruction, prompt, schema):
            select_goals(db, GoalSelection(revision=1, goals=[]), NOW)
            return AgentStep(answer="stale sporting advice", evidence_ids=[1])

    response = answer_question(db, Provider(), "synthetic", Settings(timezone="UTC"), NOW)
    assert "Личные цели изменены" in response and "stale" not in response
    assert db.info["goals_revision"] is None


def test_goal_change_fences_cached_analysis_delivery(db, db_engine):
    import asyncio

    from garmin_ai.telegram import deliver

    select_goals(db, GoalSelection(revision=0, goals=["running"]), NOW)
    db.add(
        AppState(
            key="telegram:reply:77",
            value={"text": "stale", "kind": "analysis", "goals_revision": 1},
        )
    )
    select_goals(db, GoalSelection(revision=1, goals=[]), NOW)
    db.commit()

    class Bot:
        async def send_message(self, **kwargs):
            pytest.fail("Stale goal answer must not be sent")

    asyncio.run(deliver(Bot(), db_engine, 42, "update:77", "stale"))


def test_api_selection_fences_older_telegram_message(db):
    from garmin_ai.personal_goals import telegram_goals

    select_goals(db, GoalSelection(revision=0, goals=[]), NOW)
    telegram_goals(
        db,
        "/goals бег",
        NOW + timedelta(seconds=10),
        sent_at=NOW - timedelta(seconds=1),
        update_id=5,
    )
    assert preferences(db)["goals"] == []
    telegram_goals(
        db,
        "/goals сон",
        NOW + timedelta(seconds=10),
        sent_at=NOW + timedelta(seconds=1),
        update_id=6,
    )
    assert preferences(db)["goals"] == ["sleep"]


def test_goal_read_waits_for_earlier_retrying_selection(db, db_engine):
    from garmin_ai.telegram import DiaryDeferred

    for identity, text in [(10, "/goals сон"), (11, "/goals")]:
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
    config = Settings(telegram_user_id=42, timezone="UTC")
    with pytest.raises(DiaryDeferred):
        process_message(db_engine, None, config, 11)
    process_message(db_engine, None, config, 10)
    assert "Ваши цели: сон" in process_message(db_engine, None, config, 11)


def test_goal_change_does_not_fence_local_urgent_notice(db):
    select_goals(db, GoalSelection(revision=0, goals=["running"]), NOW)

    class Provider:
        def structured(self, instruction, prompt, schema):
            select_goals(db, GoalSelection(revision=1, goals=[]), NOW)
            return AgentStep(urgent_safety=True)

    response = answer_question(db, Provider(), "synthetic", Settings(timezone="UTC"), NOW)
    assert "112" in response
    assert db.info["goals_revision"] is None


@pytest.mark.parametrize("arrival_ms,expected", [(100, []), (800, ["sleep"])])
def test_same_second_api_and_telegram_use_ingestion_order(db, arrival_ms, expected):
    from garmin_ai.models import TelegramUpdate
    from garmin_ai.personal_goals import telegram_goals

    select_goals(db, GoalSelection(revision=0, goals=[]), NOW + timedelta(milliseconds=200))
    db.add(
        TelegramUpdate(
            id=55,
            payload={},
            status="pending",
            received_at=NOW + timedelta(milliseconds=arrival_ms),
        )
    )
    db.flush()
    telegram_goals(db, "/goals сон", NOW + timedelta(seconds=5), sent_at=NOW, update_id=55)
    assert preferences(db)["goals"] == expected


@pytest.mark.parametrize("resume", [False, True])
def test_goal_change_finishes_started_multipart_answer(db, db_engine, resume):
    import asyncio
    from types import SimpleNamespace

    from garmin_ai.db import transaction
    from garmin_ai.telegram import deliver
    from garmin_ai.telegram_format import message_parts

    text = "synthetic answer " * 700
    parts = message_parts(text)
    select_goals(db, GoalSelection(revision=0, goals=["running"]), NOW)
    db.add(AppState(key="telegram:reply:88", value={"kind": "analysis", "goals_revision": 1}))
    if resume:
        db.add(AppState(key="outbox:update:88:0", value={"status": "sent", "formatted": True}))
        select_goals(db, GoalSelection(revision=1, goals=[]), NOW)
    db.commit()
    sent = []

    class Bot:
        async def send_message(self, **kwargs):
            sent.append(kwargs["text"])
            if len(sent) == 1 and not resume:
                with transaction(db_engine) as session:
                    select_goals(session, GoalSelection(revision=1, goals=[]), NOW)
            return SimpleNamespace(message_id=len(sent))

    asyncio.run(deliver(Bot(), db_engine, 42, "update:88", text))
    assert len(sent) == len(parts) - int(resume)
