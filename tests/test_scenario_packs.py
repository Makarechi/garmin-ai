from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.agent import context_for
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import Event, ModuleConfig, PendingQuestion
from garmin_ai.proactive import generate_questions
from garmin_ai.queries import list_events
from garmin_ai.scenario_packs import (
    PACKS,
    PackSelection,
    configure_scenario_pack,
    ensure_scenario_packs,
    pack_enabled,
)
from garmin_ai.telegram import scenario_keyboard
from garmin_ai.tools import call_tool

NOW = datetime(2026, 9, 20, 16, tzinfo=UTC)


def selection(row, **changes):
    values = {
        "revision": row.revision,
        "tracking_enabled": row.tracking_enabled,
        "collection_enabled": row.collection_enabled,
        "reminders_enabled": row.reminders_enabled,
        "visible": row.visible,
        "llm_enabled": row.llm_enabled,
        "outcome_goal": row.outcome_goal,
    }
    values.update(changes)
    return PackSelection(**values)


def callbacks(markup):
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


def test_first_party_pack_contracts_are_explicit_and_nonoverlapping():
    assert set(PACKS) == {
        "general_diary",
        "wellbeing",
        "sleep",
        "caffeine",
        "migraine",
        "training",
    }
    definitions = [definition for pack in PACKS.values() for definition in pack.definitions]
    assert len(definitions) == len(set(definitions))
    assert "medication" in PACKS["migraine"].definitions
    assert "caffeine" in PACKS["caffeine"].definitions


def test_new_profile_has_no_migraine_or_caffeine_actions_or_questions(db):
    create_event(
        db,
        EventInput(start=NOW - timedelta(hours=3), payload={"type": "migraine"}),
        actor="owner",
    )
    for days in range(1, 8):
        create_event(
            db,
            EventInput(
                start=NOW - timedelta(days=days),
                payload={"type": "caffeine", "beverage": "synthetic"},
            ),
            actor="owner",
        )
    configs = ensure_scenario_packs(db, legacy_install=False)
    assert configs["general_diary"].tracking_enabled
    assert not configs["migraine"].tracking_enabled
    assert not configs["caffeine"].tracking_enabled
    assert callbacks(scenario_keyboard(db)) == {"alcohol", "note"}
    generate_questions(db, Settings(timezone="UTC"), NOW)

    assert db.scalar(select(func.count()).select_from(PendingQuestion)) == 0
    with pytest.raises(PermissionError, match="migraine"):
        create_event(
            db,
            EventInput(start=NOW, payload={"type": "migraine"}),
            actor="owner",
        )


def test_legacy_profile_keeps_all_existing_actions(db):
    create_event(
        db,
        EventInput(start=NOW, payload={"type": "note", "description": "legacy"}),
        actor="owner",
    )
    configs = ensure_scenario_packs(db)

    assert all(row.tracking_enabled for row in configs.values())
    assert callbacks(scenario_keyboard(db)) == {
        "coffee",
        "migraine",
        "end",
        "medication",
        "alcohol",
        "note",
    }


def test_absent_pack_rows_preserve_pre_migration_behavior(db):
    assert db.scalar(select(func.count()).select_from(ModuleConfig)) == 0
    assert pack_enabled(db, "migraine")
    assert pack_enabled(db, "caffeine", "reminders")


def test_disabling_migraine_cancels_reminders_but_keeps_history_and_relations(db):
    migraine = create_event(
        db,
        EventInput(start=NOW - timedelta(hours=3), payload={"type": "migraine"}),
        actor="owner",
    )
    medication = create_event(
        db,
        EventInput(
            start=NOW - timedelta(hours=2),
            payload={
                "type": "medication",
                "name": "synthetic",
                "dose": None,
                "unit": None,
                "reason_event_id": migraine.id,
            },
        ),
        actor="owner",
    )
    configs = ensure_scenario_packs(db, legacy_install=True)
    generate_questions(db, Settings(timezone="UTC"), NOW)
    question = db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "migraine"))
    assert question is not None and question.status == "pending"

    configure_scenario_pack(
        db,
        "migraine",
        selection(
            configs["migraine"],
            tracking_enabled=False,
            reminders_enabled=False,
            visible=False,
        ),
    )

    db.refresh(question)
    rows = list_events(db, NOW - timedelta(days=1), NOW + timedelta(hours=1))["rows"]
    assert question.status == "cancelled"
    assert {row["id"] for row in rows} == {str(migraine.id), str(medication.id)}
    assert medication.payload["reason_event_id"] == str(migraine.id)
    assert not migraine.deleted and not medication.deleted


def test_disabled_pack_llm_access_filters_prompt_and_model_tools(db):
    migraine = create_event(
        db,
        EventInput(start=NOW - timedelta(hours=1), payload={"type": "migraine"}),
        actor="owner",
    )
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db,
        "migraine",
        selection(configs["migraine"], llm_enabled=False),
    )

    context = context_for(db, NOW)
    direct = call_tool(
        db,
        "events",
        {"start": NOW - timedelta(days=1), "end": NOW + timedelta(hours=1)},
    )
    model = call_tool(
        db,
        "events",
        {"start": NOW - timedelta(days=1), "end": NOW + timedelta(hours=1)},
        for_model=True,
    )
    timeline = call_tool(
        db,
        "timeline",
        {"start": NOW - timedelta(days=1), "end": NOW + timedelta(hours=1)},
        for_model=True,
    )

    assert str(migraine.id) not in {row["id"] for row in context["recent_events"]}
    assert [row["id"] for row in direct["rows"]] == [str(migraine.id)]
    assert model["rows"] == []
    assert all(not rows for rows in timeline["layers"].values())
    with pytest.raises(PermissionError, match="migraine"):
        call_tool(
            db,
            "analysis_migraine_windows",
            {
                "metric": "resting_heart_rate",
                "start": NOW.date() - timedelta(days=7),
                "end": NOW.date(),
            },
            for_model=True,
        )


def test_idempotent_replay_survives_pack_disable(db):
    event = EventInput(start=NOW, payload={"type": "migraine"})
    row = create_event(db, event, actor="owner", idempotency_key="message:stable")
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db,
        "migraine",
        selection(configs["migraine"], tracking_enabled=False),
    )

    replay = create_event(db, event, actor="owner", idempotency_key="message:stable")

    assert replay.id == row.id
    assert db.scalar(select(func.count()).select_from(Event)) == 1


def test_pack_capabilities_and_outcome_goal_change_independently(db):
    configs = ensure_scenario_packs(db, legacy_install=False)
    updated = configure_scenario_pack(
        db,
        "training",
        selection(
            configs["training"],
            tracking_enabled=True,
            collection_enabled=False,
            reminders_enabled=True,
            visible=False,
            llm_enabled=False,
            outcome_goal="Improve consistency",
        ),
    )

    assert updated["tracking_enabled"] is True
    assert updated["collection_enabled"] is False
    assert updated["reminders_enabled"] is True
    assert updated["visible"] is False
    assert updated["llm_enabled"] is False
    assert updated["outcome_goal"] == "Improve consistency"


def test_user_pack_cannot_replace_system_medication_safety(db):
    ensure_scenario_packs(db, legacy_install=False)
    with pytest.raises(ValueError, match="Incomplete medication"):
        create_event(
            db,
            EventInput(
                start=NOW,
                source="inferred",
                status="inferred",
                payload={"type": "medication", "name": None, "dose": None, "unit": None},
            ),
            actor="model",
        )

    assert db.scalar(select(func.count()).select_from(Event)) == 0
