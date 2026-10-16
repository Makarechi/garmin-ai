from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from garmin_ai import agent
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.ingest import ingest
from garmin_ai.models import AppState, SourcePayload, TimelineInterval
from garmin_ai.normalize import PARSER_VERSION
from garmin_ai.tools import ReplayUnavailable, call_tool

DAY = date(2026, 10, 16)
START = datetime(2026, 10, 15, 21, 42, tzinfo=UTC)
END = datetime(2026, 10, 16, 5, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def stored_night(db, tmp_path):
    result = ingest(
        db,
        LocalArchive(tmp_path),
        "sleep",
        str(DAY),
        {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": int(START.timestamp() * 1000),
                "sleepEndTimestampGMT": int(END.timestamp() * 1000),
            }
        },
        "Europe/Bratislava",
    )
    return db.get(SourcePayload, UUID(result["source_ref"]))


def timing(db, **kwargs):
    return call_tool(
        db,
        "analysis_sleep",
        {
            "start": str(DAY),
            "end": str(DAY),
            "timing_only": True,
            **kwargs,
        },
        for_model=True,
    )


class NoModel:
    def structured(self, *args):
        raise AssertionError("A recorded sleep time does not require another model round")


@pytest.mark.parametrize(
    "question,field",
    [
        ("Когда я заснул сегодня?", "start"),
        ("Во сколько я сегодня уснула?", "start"),
        ("В какое время я лёг спать сегодня?", "start"),
        ("Когда проснулась сегодня?", "end"),
        ("Когда я сегодня проснулся?", "end"),
    ],
)
def test_simple_question_selection(question, field):
    assert agent.simple_sleep_time_field(question) == field


@pytest.mark.parametrize(
    "question",
    [
        "Когда я заснул вчера?",
        "Когда я заснул сегодня и почему плохо спал?",
        "Когда я заснул сегодня? Сейчас внезапная сильная боль.",
        "Когда я заснул сегодня и запиши кофе?",
        "Когда я заснул?",
        "Когда я сегодня заснул сегодня?",
    ],
)
def test_other_questions_stay_in_the_regular_safety_and_analysis_flow(question):
    assert agent.simple_sleep_time_field(question) is None


@pytest.mark.parametrize(
    "field,expected", [("заснул", "15.10.2026 в 23:42"), ("проснулся", "16.10.2026 в 07:30")]
)
def test_exact_question_reads_current_source_without_model_or_full_freshness(
    db, tmp_path, monkeypatch, field, expected
):
    stored_night(db, tmp_path)
    from garmin_ai import queries, replay

    monkeypatch.setattr(
        queries, "data_freshness", lambda *args, **kwargs: pytest.fail("Unrelated freshness scan")
    )
    monkeypatch.setattr(
        replay, "replay_pending_condition", lambda: pytest.fail("Unrelated archive scan")
    )
    budget = agent.AnalysisBudget()
    response = agent.answer_question(
        db, NoModel(), f"Когда я {field} сегодня?", Settings(), NOW, budget=budget, update_id=1
    )
    assert expected in response
    assert budget.model_calls == 0
    pending = db.get(AppState, "analysis:conversation:pending").value["turn"]
    assert pending["answer"] == response
    assert pending["specs"][0]["arguments"]["timing_only"] is True


def test_current_night_can_be_read_while_unrelated_history_is_waiting(db, tmp_path):
    stored_night(db, tmp_path)
    old = SourcePayload(
        source="garmin_connect",
        endpoint="stress",
        source_key="2025-01-01",
        payload_hash="synthetic-old",
        payload={},
        archive_key="synthetic",
        parser_version=PARSER_VERSION - 1,
        status="normalized",
    )
    db.add(old)
    db.flush()
    with pytest.raises(ReplayUnavailable):
        call_tool(db, "analysis_sleep", {"start": str(DAY), "end": str(DAY)})
    assert timing(db)["status"] == "available"


@pytest.mark.parametrize(
    "corruption",
    [
        "old_parser",
        "failed",
        "missing_source",
        "wrong_source",
        "wrong_day",
        "superseded",
        "wrong_hash",
        "wrong_time",
        "failed_current_parser",
    ],
)
def test_unverified_or_outdated_source_never_supplies_a_time(db, tmp_path, corruption):
    raw = stored_night(db, tmp_path)
    night = db.get(TimelineInterval, f"sleep:{DAY}")
    state = db.get(AppState, f"ingest:garmin_connect:sleep:{DAY}")
    if corruption == "old_parser":
        raw.parser_version -= 1
    elif corruption == "failed":
        raw.status = "error"
    elif corruption == "missing_source":
        night.evidence = {}
    elif corruption == "wrong_source":
        raw.source = "synthetic-other"
    elif corruption == "wrong_day":
        raw.source_key = "2026-10-15"
    elif corruption == "superseded":
        state.value = {**state.value, "source_ref": "different"}
    elif corruption == "wrong_hash":
        state.value = {**state.value, "hash": "different"}
    elif corruption == "wrong_time":
        night.start += timedelta(minutes=1)
    elif corruption == "failed_current_parser":
        metadata = db.get(AppState, f"ingest-meta:{raw.id}")
        metadata.value = {**metadata.value, "failed_parser_version": PARSER_VERSION}
    db.flush()
    result = timing(db)
    assert result["status"] == "unverified_source"
    assert result["rows"][0]["main_interval"] is None


def test_missing_today_never_selects_previous_night(db, tmp_path):
    stored_night(db, tmp_path)
    response = agent.answer_question(
        db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW + timedelta(days=1)
    )
    assert "17.10.2026" in response and "пока не подтверждено" in response
    assert "23:42" not in response and "лимит" not in response


@pytest.mark.parametrize("arguments", [{"end": "2026-10-17"}, {"nap_policy": "include_confirmed"}])
def test_timing_mode_requires_one_day_and_excludes_naps(db, arguments):
    with pytest.raises(ValueError):
        timing(db, **arguments)


def test_disabled_sleep_sharing_still_blocks_direct_answer(db, tmp_path):
    from garmin_ai.scenario_packs import ensure_scenario_packs

    stored_night(db, tmp_path)
    ensure_scenario_packs(db)
    from sqlalchemy import select

    from garmin_ai.models import ModuleConfig

    row = db.scalar(select(ModuleConfig).where(ModuleConfig.pack_key == "sleep"))
    row.llm_enabled = False
    db.flush()
    response = agent.answer_question(db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW)
    assert "настройками доступа" in response and "23:42" not in response


def test_forgetting_during_lookup_discards_the_answer(db, tmp_path, monkeypatch):
    from garmin_ai.conversation import forget_conversation

    stored_night(db, tmp_path)
    original = agent.call_tool

    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        forget_conversation(db)
        return result

    monkeypatch.setattr(agent, "call_tool", read)
    response = agent.answer_question(
        db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, update_id=1
    )
    assert "Контекст разговора удалён" in response and "23:42" not in response
    assert db.get(AppState, "analysis:conversation:pending") is None


def test_replay_generation_change_discards_the_answer(db, tmp_path, monkeypatch):
    from garmin_ai.normalize import upsert
    from garmin_ai.replay import REPLAY_NOTICE

    stored_night(db, tmp_path)
    original = agent.call_tool

    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        upsert(db, AppState, {"key": "replay:generation", "value": {"id": "new"}}, ["key"])
        return result

    monkeypatch.setattr(agent, "call_tool", read)
    response = agent.answer_question(
        db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, update_id=1
    )
    assert response == REPLAY_NOTICE
    assert db.get(AppState, "analysis:conversation:pending") is None


def test_busy_projection_writer_returns_replay_notice_without_waiting(db, db_engine, tmp_path):
    from sqlalchemy import text

    from garmin_ai.replay import REPLAY_NOTICE

    stored_night(db, tmp_path)
    db.commit()
    with db_engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        response = agent.answer_question(db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW)
    assert response == REPLAY_NOTICE
