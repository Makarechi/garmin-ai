import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from garmin_ai.accounts import bind_account, owner, profile_fingerprint
from garmin_ai.agent import context_for, interpret
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event, update_event
from garmin_ai.models import (
    AppState,
    ChannelBinding,
    Event,
    Insight,
    Job,
    ModuleConfig,
    PendingQuestion,
    SourceConnection,
    SourcePayload,
)
from garmin_ai.proactive import generate_questions, pending_insight_notices, reserve_insight_notice
from garmin_ai.queries import list_events
from garmin_ai.scenario_packs import (
    PACKS,
    PackSelection,
    configure_scenario_pack,
    ensure_scenario_packs,
    garmin_collection_enabled,
    pack_enabled,
    question_enabled,
)
from garmin_ai.telegram import callback_pack, scenario_keyboard
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


@pytest.mark.parametrize(
    "pack,name,arguments",
    [
        ("sleep", "health_snapshot", {"day": NOW.date()}),
        ("sleep", "health_range", {"start": NOW.date(), "end": NOW.date()}),
        (
            "sleep",
            "personal_baseline",
            {"metric": "sleep_score", "start": NOW.date(), "end": NOW.date()},
        ),
        ("training", "activities", {"start": NOW, "end": NOW + timedelta(hours=1)}),
        (
            "wellbeing",
            "metric_series",
            {"metric": "stress_score", "start": NOW, "end": NOW + timedelta(hours=1)},
        ),
        (
            "training",
            "metric_series",
            {"metric": "steps_bucket", "start": NOW, "end": NOW + timedelta(hours=1)},
        ),
        (
            "wellbeing",
            "analysis_event_windows",
            {
                "event_type": "caffeine",
                "metric": "stress_score",
                "start": NOW,
                "end": NOW + timedelta(hours=1),
            },
        ),
        ("sleep", "analysis_coffee_sleep", {"start": NOW.date(), "end": NOW.date()}),
        (
            "sleep",
            "analysis_lagged_association",
            {
                "metric_a": "sleep_score",
                "metric_b": "stress_avg",
                "start": NOW.date(),
                "end": NOW.date(),
                "lags": [0],
            },
        ),
    ],
)
def test_model_tools_enforce_every_exposed_pack(db, pack, name, arguments):
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(db, pack, selection(configs[pack], llm_enabled=False))
    with pytest.raises(PermissionError, match=pack):
        call_tool(db, name, arguments, for_model=True)


def callbacks(markup):
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


def test_first_party_pack_contracts_are_explicit_and_nonoverlapping():
    assert PACKS["wellbeing"].rules == frozenset({"context_follow_up"})
    assert PACKS["sleep"].rules == frozenset()
    assert PACKS["training"].rules == frozenset()
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


def test_fallback_coffee_callback_uses_caffeine_pack():
    assert callback_pack("coffee:unspecified") == "caffeine"


def test_migraine_followup_requires_tracking_even_when_reminders_enabled(db):
    create_event(
        db,
        EventInput(start=NOW - timedelta(hours=3), payload={"type": "migraine"}),
        actor="owner",
    )
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db,
        "migraine",
        selection(configs["migraine"], tracking_enabled=False, reminders_enabled=True),
    )
    generate_questions(db, Settings(timezone="UTC"), NOW)
    assert db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "migraine")) is None


def test_caffeine_tracking_disable_cancels_and_stops_reminders(db):
    for day in range(1, 8):
        create_event(
            db,
            EventInput(
                start=NOW - timedelta(days=day),
                payload={"type": "caffeine", "beverage": "synthetic"},
            ),
            actor="owner",
        )
    configs = ensure_scenario_packs(db, legacy_install=True)
    generate_questions(db, Settings(timezone="UTC"), NOW)
    pending = db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "caffeine"))
    assert pending is not None and pending.status == "pending"

    configure_scenario_pack(
        db,
        "caffeine",
        selection(configs["caffeine"], tracking_enabled=False, reminders_enabled=True),
    )
    assert pending.status == "cancelled"
    assert not question_enabled(db, "caffeine", "reminders")
    generate_questions(db, Settings(timezone="UTC"), NOW + timedelta(minutes=30))
    assert (
        db.scalar(
            select(func.count())
            .select_from(PendingQuestion)
            .where(PendingQuestion.kind == "caffeine", PendingQuestion.status == "pending")
        )
        == 0
    )


def test_disabling_llm_discards_unclassified_diary_clarification(db):
    configs = ensure_scenario_packs(db, legacy_install=True)
    db.add(
        AppState(
            key="conversation:pending",
            value={"text": "synthetic note", "question": "Clarify?", "messages": []},
        )
    )
    db.flush()
    configure_scenario_pack(
        db, "general_diary", selection(configs["general_diary"], llm_enabled=False)
    )
    assert db.get(AppState, "conversation:pending") is None


def test_queued_garmin_jobs_release_scan_state_when_collection_is_disabled(db, db_engine, tmp_path):
    from garmin_ai.activity_sync import schedule_scans
    from garmin_ai.archive import LocalArchive
    from garmin_ai.backfill import schedule_history
    from garmin_ai.sync import run_garmin_job

    account = profile_fingerprint({"profileId": 12345})
    bind_account(db, account)
    settings = Settings(backfill_days=1, timezone="UTC")
    schedule_scans(db, settings, NOW)
    schedule_history(db, settings, NOW)
    activity_job = db.scalar(select(Job).where(Job.kind == "garmin_activities"))
    sleep_job = db.scalar(
        select(Job).where(
            Job.kind == "garmin_endpoint", Job.payload["endpoint"].as_string() == "sleep"
        )
    )
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db, "training", selection(configs["training"], collection_enabled=False)
    )
    configure_scenario_pack(db, "sleep", selection(configs["sleep"], collection_enabled=False))
    db.commit()

    reader = SimpleNamespace(account_fingerprint=lambda: account)
    archive = LocalArchive(tmp_path)
    run_garmin_job(db_engine, reader, archive, settings, "garmin_activities", activity_job.payload)
    run_garmin_job(db_engine, reader, archive, settings, "garmin_endpoint", sleep_job.payload)
    db.expire_all()
    assert db.get(AppState, activity_job.payload["scan_key"]).value["status"] == "disabled"
    assert db.get(AppState, sleep_job.payload["sync_window"]).value["status"] == "disabled"


def test_fit_job_stays_retryable_while_collection_is_disabled(db, db_engine, tmp_path):
    from garmin_ai.archive import LocalArchive
    from garmin_ai.sync import GarminCollectionDisabled, run_garmin_job

    account = profile_fingerprint({"profileId": 12345})
    bind_account(db, account)
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db, "training", selection(configs["training"], collection_enabled=False)
    )
    db.commit()
    reader = SimpleNamespace(
        account_fingerprint=lambda: account,
        call=lambda *args, **kwargs: pytest.fail("disabled FIT must not be downloaded"),
    )
    with pytest.raises(GarminCollectionDisabled):
        run_garmin_job(
            db_engine,
            reader,
            LocalArchive(tmp_path),
            Settings(),
            "garmin_fit",
            {"activity_id": "synthetic"},
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


def test_fresh_channel_binding_does_not_enable_legacy_profile(db):
    db.add(
        ChannelBinding(
            owner_id=owner(db).id,
            channel="telegram",
            channel_instance_id="primary",
            external_id="42",
            confirmation_method="telegram_get_updates",
        )
    )
    db.flush()

    configs = ensure_scenario_packs(db)

    assert configs["general_diary"].tracking_enabled
    assert not configs["migraine"].tracking_enabled
    assert not configs["migraine"].llm_enabled


def test_fresh_source_connection_does_not_enable_legacy_profile(db):
    db.add(
        SourceConnection(
            owner_id=owner(db).id,
            provider="garmin",
            namespace="synthetic-profile-v1",
            external_id="synthetic-owner",
            confirmation_method="local_login",
        )
    )
    db.flush()

    configs = ensure_scenario_packs(db)

    assert configs["general_diary"].tracking_enabled
    assert not configs["training"].tracking_enabled


def test_mcp_bootstrap_recreates_clean_install_pack_defaults(db, db_engine):
    from garmin_ai.mcp_server import initialize_identity

    initialize_identity(db_engine, Settings())
    rows = {row.pack_key: row for row in db.scalars(select(ModuleConfig))}
    assert set(rows) == set(PACKS)
    assert rows["general_diary"].collection_enabled
    assert not rows["sleep"].collection_enabled
    assert not rows["training"].collection_enabled


def test_absent_pack_rows_preserve_pre_migration_behavior(db):
    assert db.scalar(select(func.count()).select_from(ModuleConfig)) == 0
    assert pack_enabled(db, "migraine")
    assert pack_enabled(db, "caffeine", "reminders")


def test_collection_opt_out_blocks_scheduling_and_raw_archive(db, tmp_path):
    from garmin_ai.archive import LocalArchive
    from garmin_ai.ingest import ingest
    from garmin_ai.sync import schedule_sync

    ensure_scenario_packs(db, legacy_install=False)
    assert garmin_collection_enabled(db, "hydration")
    assert not garmin_collection_enabled(db, "daily")
    assert not garmin_collection_enabled(db, "sleep")
    with pytest.raises(ValueError, match="Unknown Garmin endpoint"):
        garmin_collection_enabled(db, "unclassified_sensitive_feed")

    schedule_sync(db, Settings(timezone="UTC", backfill_days=0), NOW + timedelta(hours=3))
    queued = list(db.scalars(select(Job)))
    assert queued
    assert all(job.payload.get("endpoint") == "hydration" for job in queued)

    archive = LocalArchive(tmp_path)
    result = ingest(db, archive, "sleep", "2026-09-20", {"sensitive": "synthetic"}, "UTC")
    assert result == {"status": "disabled"}
    assert db.scalar(select(func.count()).select_from(SourcePayload)) == 0
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_client_cannot_use_wearable_source_to_bypass_tracking_opt_out(db):
    configs = ensure_scenario_packs(db, legacy_install=False)
    configure_scenario_pack(
        db,
        "sleep",
        selection(configs["sleep"], tracking_enabled=False, collection_enabled=True),
    )
    with pytest.raises(PermissionError, match="sleep"):
        create_event(
            db,
            EventInput(
                start=NOW,
                source="wearable",
                payload={"type": "nap", "description": "synthetic"},
            ),
            actor="api",
        )


def test_disabling_llm_pack_forgets_prior_analysis_turns(db):
    from garmin_ai.conversation import KEY

    configs = ensure_scenario_packs(db, legacy_install=True)
    db.add(
        AppState(
            key=KEY,
            value={
                "epoch": "old",
                "turns": [{"update_id": 42, "question": "synthetic private fact"}],
            },
        )
    )
    db.flush()
    configure_scenario_pack(db, "migraine", selection(configs["migraine"], llm_enabled=False))
    state = db.get(AppState, KEY, populate_existing=True).value
    assert state["turns"] == []
    assert state["epoch"] != "old"


def test_disabling_diary_reminders_cancels_context_prompts(db):
    from garmin_ai.proactive import add_question

    configs = ensure_scenario_packs(db, legacy_install=True)
    add_question(db, "context", "Synthetic prompt", {}, 0.9, "synthetic-context", NOW)
    assert question_enabled(db, "context", "reminders")
    configure_scenario_pack(
        db,
        "general_diary",
        selection(configs["general_diary"], reminders_enabled=False),
    )
    prompt = db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "context"))
    assert prompt.status == "cancelled"
    assert not question_enabled(db, "context", "reminders")


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


def test_model_prompt_filters_freshness_by_pack_consent(db, monkeypatch):
    from garmin_ai.agent import AgentStep, answer_question

    configs = ensure_scenario_packs(db, legacy_install=True)
    for key in ("sleep", "training"):
        configure_scenario_pack(db, key, selection(configs[key], llm_enabled=False))
    monkeypatch.setattr(
        "garmin_ai.queries.data_freshness",
        lambda *args, **kwargs: {
            "channels": {
                "sleep_score": {"source_ref": "private-sleep"},
                "training_readiness_score": {"source_ref": "private-training"},
                "stress_score": {"source_ref": "allowed-wellbeing"},
            }
        },
    )

    class CapturingProvider:
        def structured(self, instruction, prompt, schema):
            context = json.loads(prompt)["quality_context"]
            assert context == {"stress_score": {"source_ref": "allowed-wellbeing"}}
            return AgentStep(urgent_safety=True)

    assert "112" in answer_question(db, CapturingProvider(), "synthetic", Settings(), NOW)


def test_model_freshness_tool_filters_disabled_packs(db, monkeypatch):
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(db, "sleep", selection(configs["sleep"], llm_enabled=False))
    monkeypatch.setattr(
        "garmin_ai.queries.data_freshness",
        lambda *args, **kwargs: {
            "channels": {
                "sleep_score": {"source_ref": "private-sleep"},
                "stress_score": {"source_ref": "allowed-wellbeing"},
            },
            "endpoints": {"sleep": {"source_ref": "private-sleep"}},
            "checked_at": NOW.isoformat(),
        },
    )

    result = call_tool(db, "data_freshness", {}, for_model=True)
    assert result["channels"] == {"stress_score": {"source_ref": "allowed-wellbeing"}}
    assert "endpoints" not in result


def test_disabled_pack_filters_unbound_form_and_explicit_uuid(db):
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
    db.add(
        AppState(
            key="conversation:pending",
            value={
                "text": "Добавить начало мигрени",
                "question": "Когда началась?",
                "event_ids": [],
                "button": "migraine",
                "pack": "migraine",
                "created_at": NOW.isoformat(),
            },
        )
    )
    db.flush()

    assert context_for(db, NOW)["pending_clarification"] is None

    class Provider:
        def structured(self, *args):
            pytest.fail("A disabled-pack event must not reach the model")

    result = interpret(db, Provider(), f"исправь {migraine.id}", Settings(), NOW)
    assert result.intent == "clarify"


def test_model_event_limit_is_applied_after_pack_policy(db):
    disallowed = create_event(
        db,
        EventInput(start=NOW - timedelta(hours=2), payload={"type": "migraine"}),
        actor="owner",
    )
    allowed = create_event(
        db,
        EventInput(
            start=NOW - timedelta(hours=1),
            payload={"type": "note", "description": "allowed"},
        ),
        actor="owner",
    )
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db,
        "migraine",
        selection(configs["migraine"], llm_enabled=False),
    )
    db.info["llm_access"] = True
    try:
        result = list_events(db, NOW - timedelta(days=1), NOW, limit=1)
    finally:
        db.info.pop("llm_access", None)

    assert [row["id"] for row in result["rows"]] == [str(allowed.id)]
    assert str(disallowed.id) not in {row["id"] for row in result["rows"]}


def test_correction_cannot_move_event_into_disabled_pack(db):
    note = create_event(
        db,
        EventInput(start=NOW, payload={"type": "note", "description": "before"}),
        actor="owner",
    )
    ensure_scenario_packs(db, legacy_install=False)

    with pytest.raises(PermissionError, match="migraine"):
        update_event(
            db,
            note.id,
            EventInput(start=NOW, payload={"type": "migraine"}),
            revision=note.revision,
            actor="owner",
        )


def test_client_source_cannot_select_collection_capability(db):
    configs = ensure_scenario_packs(db, legacy_install=False)
    configure_scenario_pack(
        db,
        "migraine",
        selection(
            configs["migraine"],
            tracking_enabled=False,
            collection_enabled=True,
        ),
    )
    event = EventInput(
        start=NOW,
        source="wearable",
        payload={"type": "migraine"},
    )

    with pytest.raises(PermissionError, match="migraine"):
        create_event(db, event, actor="api")
    assert create_event(db, event, actor="wearable:trusted-device").kind == "migraine"


def test_context_question_requires_writable_general_diary(db):
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db,
        "general_diary",
        selection(configs["general_diary"], tracking_enabled=False),
    )
    from garmin_ai.scenario_packs import question_enabled

    assert not question_enabled(db, "context", "reminders")


def test_context_question_honors_general_diary_reminder_opt_out(db):
    from garmin_ai.proactive import add_question
    from garmin_ai.scenario_packs import question_enabled

    configs = ensure_scenario_packs(db, legacy_install=True)
    add_question(db, "context", "Synthetic follow-up", {}, 0.9, "context-opt-out", NOW)
    configure_scenario_pack(
        db,
        "general_diary",
        selection(configs["general_diary"], reminders_enabled=False),
    )

    assert not question_enabled(db, "context", "reminders")
    question = db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "context"))
    assert question.status == "cancelled"


def test_migraine_tracking_opt_out_cancels_pending_prompt(db):
    from garmin_ai.proactive import add_question
    from garmin_ai.scenario_packs import question_enabled

    configs = ensure_scenario_packs(db, legacy_install=True)
    add_question(db, "migraine", "Synthetic follow-up", {}, 0.9, "migraine-opt-out", NOW)
    configure_scenario_pack(
        db,
        "migraine",
        selection(configs["migraine"], tracking_enabled=False),
    )
    assert not question_enabled(db, "migraine", "reminders")
    question = db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "migraine"))
    assert question.status == "cancelled"


@pytest.mark.parametrize("disabled", [{"tracking_enabled": False}, {"reminders_enabled": False}])
def test_disabled_pack_suppresses_previously_accepted_insight(db, disabled):
    insight = Insight(
        category="trend",
        statement="synthetic sleep trend",
        evidence={},
        sample_size=28,
        effect_size=1,
        status="accepted",
        dedup_key="trend:sleep_score:2026:38",
        generated_at=NOW,
    )
    db.add(insight)
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db,
        "sleep",
        selection(configs["sleep"], **disabled),
    )
    db.flush()

    assert pending_insight_notices(db, NOW) == []
    assert not reserve_insight_notice(db, Settings(), NOW, insight)


def test_generic_health_tools_require_all_exposed_pack_consents(db):
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db,
        "sleep",
        selection(configs["sleep"], llm_enabled=False),
    )

    with pytest.raises(PermissionError, match="sleep"):
        call_tool(db, "health_snapshot", {"day": NOW.date()}, for_model=True)
    with pytest.raises(PermissionError, match="sleep"):
        call_tool(
            db,
            "metric_series",
            {
                "metric": "sleep_score",
                "start": NOW - timedelta(days=1),
                "end": NOW,
            },
            for_model=True,
        )

    configure_scenario_pack(
        db,
        "sleep",
        selection(configs["sleep"], llm_enabled=True),
    )
    configure_scenario_pack(
        db,
        "general_diary",
        selection(configs["general_diary"], llm_enabled=False),
    )
    with pytest.raises(PermissionError, match="general_diary"):
        call_tool(db, "health_snapshot", {"day": NOW.date()}, for_model=True)


def test_model_tools_gate_steps_and_migraine_insights(db):
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db,
        "training",
        selection(configs["training"], llm_enabled=False),
    )
    with pytest.raises(PermissionError, match="training"):
        call_tool(
            db,
            "metric_series",
            {"metric": "steps_bucket", "start": NOW - timedelta(days=1), "end": NOW},
            for_model=True,
        )
    configure_scenario_pack(
        db,
        "migraine",
        selection(configs["migraine"], llm_enabled=False),
    )
    with pytest.raises(PermissionError, match="migraine"):
        call_tool(db, "insights_list", {"limit": 10}, for_model=True)


def test_hydration_and_steps_require_their_own_model_consents(db):
    configs = ensure_scenario_packs(db, legacy_install=True)
    configure_scenario_pack(
        db,
        "general_diary",
        selection(configs["general_diary"], llm_enabled=False),
    )
    with pytest.raises(PermissionError, match="general_diary"):
        call_tool(db, "health_snapshot", {"day": NOW.date()}, for_model=True)
    with pytest.raises(PermissionError, match="general_diary"):
        call_tool(
            db,
            "metric_series",
            {"metric": "hydration_ml", "start": NOW - timedelta(days=1), "end": NOW},
            for_model=True,
        )
    configure_scenario_pack(
        db,
        "training",
        selection(configs["training"], llm_enabled=False),
    )
    with pytest.raises(PermissionError, match="training"):
        call_tool(
            db,
            "metric_series",
            {"metric": "steps_bucket", "start": NOW - timedelta(days=1), "end": NOW},
            for_model=True,
        )


def test_disabling_migraine_tracking_cancels_pending_reminders(db):
    configs = ensure_scenario_packs(db, legacy_install=True)
    question = PendingQuestion(
        kind="migraine",
        text="Synthetic reminder",
        evidence={},
        priority=0.9,
        earliest_send_at=NOW,
        expires_at=NOW + timedelta(days=1),
        dedup_key="synthetic-migraine-reminder",
    )
    db.add(question)
    db.flush()

    configure_scenario_pack(
        db,
        "migraine",
        selection(configs["migraine"], tracking_enabled=False, reminders_enabled=True),
    )
    assert question.status == "cancelled"
    assert not question_enabled(db, "migraine", "reminders")


def test_pack_discovery_only_advertises_implemented_reminder_rules():
    assert "context_follow_up" in PACKS["wellbeing"].rules
    assert PACKS["sleep"].rules == frozenset()
    assert PACKS["training"].rules == frozenset()


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
