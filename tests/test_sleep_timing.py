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


def stored_night(db, tmp_path, calendar_date=str(DAY)):
    result = ingest(
        db,
        LocalArchive(tmp_path),
        "sleep",
        str(DAY),
        {
            "dailySleepDTO": {
                "calendarDate": calendar_date,
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


def test_literal_sleep_question_needs_no_model_or_diary_context(db, monkeypatch):
    monkeypatch.setattr(agent, "context_for", lambda *args: pytest.fail("Unrelated diary scan"))
    budget = agent.AnalysisBudget()
    command = agent.interpret(
        db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, budget=budget
    )
    assert command.intent == "question" and not command.events
    assert budget.model_calls == 0


def test_timing_wait_respects_time_spent_finishing_prior_context(db, monkeypatch):
    clock = [0]
    monkeypatch.setattr(agent, "monotonic", lambda: clock[0])
    monkeypatch.setattr(agent, "call_tool", lambda *args, **kwargs: pytest.fail("Expired read"))
    budget = agent.AnalysisBudget()

    def finish_context():
        clock[0] = agent.ANALYSIS_SECONDS

    response = agent.answer_question(
        db,
        NoModel(),
        "Когда я заснул сегодня?",
        Settings(),
        NOW,
        before_model=finish_context,
        budget=budget,
    )
    assert response == agent.ANALYSIS_TIMEOUT_NOTICE
    assert "sleep_timing_wait_ms" not in db.info


@pytest.mark.parametrize(
    "question,intent",
    [
        ("Когда я заснул сегодня? Сейчас внезапная сильная боль.", "safety"),
        ("Когда я заснул сегодня и запиши кофе?", "clarify"),
    ],
)
def test_appended_symptoms_or_actions_keep_normal_interpretation(db, question, intent):
    class Provider:
        calls = 0

        def structured(self, instruction, prompt, schema):
            self.calls += 1
            assert schema is agent.Interpretation
            return agent.Interpretation(intent=intent, confidence=1)

    provider = Provider()
    command = agent.interpret(db, provider, question, Settings(), NOW)
    assert command.intent == intent and provider.calls == 1


@pytest.mark.parametrize(
    "destination,expected", [("telegram:primary", True), ("telegram:other", False)]
)
def test_literal_question_preserves_optional_refinement_channel_scope(db, destination, expected):
    from garmin_ai.pending_state import pending_key

    db.info["channel_destination_instance_id"] = "telegram:primary"
    db.add(
        AppState(
            key=pending_key(db),
            value={
                "created_at": NOW.isoformat(),
                "optional_refinement": True,
                "channel_instance_id": destination,
            },
        )
    )
    db.flush()
    command = agent.interpret(db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW)
    assert command._dismiss_refinement is expected


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
    "case",
    [
        "available",
        "missing",
        "source_changed",
        "generation_changed",
        "generation_and_source_changed",
        "forgotten",
        "goals_changed",
    ],
)
def test_telegram_question_reaches_delivery_during_unrelated_replay(db, db_engine, tmp_path, case):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import text

    from garmin_ai.replay import REPLAY_NOTICE, invalidate_outputs
    from garmin_ai.telegram import deliver, process_message, save_update

    if case != "missing":
        stored_night(db, tmp_path)
    db.add(
        SourcePayload(
            source="garmin_connect",
            endpoint="stress",
            source_key="2025-01-01",
            payload_hash="synthetic-old",
            payload={},
            archive_key="synthetic",
            parser_version=PARSER_VERSION - 1,
            status="normalized",
        )
    )
    save_update(
        db,
        {
            "update_id": 991,
            "message": {
                "message_id": 991,
                "date": int(NOW.timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "Когда я заснул сегодня?",
            },
        },
        42,
    )
    db.commit()

    class Provider:
        calls = 0

        def structured(self, instruction, prompt, schema):
            assert schema is agent.Interpretation
            self.calls += 1
            return agent.Interpretation(intent="question", confidence=1)

    provider = Provider()
    response = process_message(db_engine, provider, Settings(telegram_user_id=42), 991)
    assert provider.calls == 0
    if case == "missing":
        assert "пока не подтверждено" in response
    else:
        assert "15.10.2026 в 23:42" in response
    if case in {"source_changed", "generation_and_source_changed"}:
        db.get(TimelineInterval, f"sleep:{DAY}").confirmed = False
        db.commit()
    if case in {"generation_changed", "generation_and_source_changed"}:
        invalidate_outputs(db)
        db.commit()
    if case == "forgotten":
        from garmin_ai.conversation import forget_conversation

        forget_conversation(db)
        db.commit()
    if case == "goals_changed":
        from garmin_ai.personal_goals import GoalSelection, select_goals

        select_goals(db, GoalSelection(revision=0, goals=["sleep"]), NOW)
        db.commit()
    sent = []

    class Bot:
        async def send_message(self, **kwargs):
            with db_engine.begin() as probe:
                assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104619)"))
            sent.append(kwargs["text"])
            return SimpleNamespace(message_id=991)

    asyncio.run(deliver(Bot(), db_engine, 42, "update:991", response))
    if case in {"source_changed", "generation_and_source_changed"}:
        assert sent == [REPLAY_NOTICE]
    elif case in {"forgotten", "goals_changed"}:
        assert sent == []
    else:
        assert len(sent) == 1 and response in sent[0]


@pytest.mark.parametrize("snapshot", [None, {}, {"day": 1, "timezone": "UTC", "hash": "x"}])
def test_malformed_timing_delivery_snapshot_is_not_current(db, snapshot):
    from garmin_ai.sleep_timing import timing_projection_current

    assert not timing_projection_current(db, snapshot)


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


def test_payload_for_a_different_wake_day_never_supplies_todays_time(db, tmp_path):
    stored_night(db, tmp_path, calendar_date="2026-10-15")
    assert timing(db)["status"] == "unverified_source"
    response = agent.answer_question(db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW)
    assert "пока не подтверждено" in response and "23:42" not in response


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


@pytest.mark.parametrize("holder", ["pg_advisory_xact_lock", "pg_advisory_xact_lock_shared"])
def test_busy_projection_reader_or_writer_returns_notice_after_bounded_wait(
    db, db_engine, tmp_path, holder, monkeypatch
):
    from sqlalchemy import text

    from garmin_ai import sleep_timing
    from garmin_ai.replay import REPLAY_NOTICE

    monkeypatch.setattr(sleep_timing, "TIMING_FENCE_WAIT_MS", 50)

    stored_night(db, tmp_path)
    db.commit()
    db.execute(text("SET LOCAL statement_timeout = '1500ms'"))
    with db_engine.begin() as connection:
        connection.execute(text(f"SELECT {holder}(72104619)"))
        response = agent.answer_question(db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW)
    assert response == REPLAY_NOTICE


def test_overlapping_timing_answers_do_not_upgrade_shared_locks(
    db, db_engine, tmp_path, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from garmin_ai import sleep_timing
    from garmin_ai.replay import REPLAY_NOTICE

    monkeypatch.setattr(sleep_timing, "TIMING_FENCE_WAIT_MS", 50)

    stored_night(db, tmp_path)
    db.commit()
    original = agent.call_tool
    overlapping = []

    def read(session, *args, **kwargs):
        result = original(session, *args, **kwargs)
        if session is db:
            # Start another answer after the first read, before its later
            # revision/epoch checks and conversation write acquire their locks.
            with Session(db_engine) as second:
                second.execute(text("SET LOCAL statement_timeout = '1500ms'"))
                overlapping.append(
                    agent.answer_question(
                        second, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, update_id=2
                    )
                )
                second.rollback()
        return result

    monkeypatch.setattr(agent, "call_tool", read)
    response = agent.answer_question(
        db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, update_id=1
    )
    assert "15.10.2026 в 23:42" in response
    assert overlapping == [REPLAY_NOTICE]


@pytest.mark.parametrize("kind", ["read", "answer", "delivery"])
def test_timing_request_joins_the_replay_queue(db, db_engine, tmp_path, monkeypatch, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import sleep

    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from garmin_ai import sleep_timing

    stored_night(db, tmp_path)
    db.commit()
    monkeypatch.setattr(sleep_timing, "TIMING_FENCE_WAIT_MS", 5000)
    started = Event()
    pids = []

    def queued_request():
        if kind in {"read", "answer"}:
            with Session(db_engine) as session:
                pids.append(session.scalar(text("SELECT pg_backend_pid()")))
                started.set()
                if kind == "read":
                    return timing(session)["status"]
                return agent.answer_question(
                    session, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, update_id=1
                )
        with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            pids.append(connection.scalar(text("SELECT pg_backend_pid()")))
            previous = connection.scalar(text("SELECT current_setting('lock_timeout')"))
            started.set()
            acquired = sleep_timing.acquire_timing_delivery_fence(connection)
            try:
                assert connection.scalar(text("SELECT current_setting('lock_timeout')")) == previous
                return acquired
            finally:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock_shared(72104619)"))

    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(db_engine) as writer:
            writer.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
            pending = executor.submit(queued_request)
            assert started.wait(3)
            for _ in range(200):
                with db_engine.connect() as probe:
                    waiting = probe.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid "
                            "AND locktype='advisory' AND objid=72104619 AND NOT granted)"
                        ),
                        {"pid": pids[0]},
                    )
                if waiting:
                    break
                if pending.done():
                    pending.result()
                    pytest.fail("The timing request did not join the lock queue")
                sleep(0.01)
            else:
                pytest.fail("The timing request never joined the lock queue")
            from garmin_ai.replay import invalidate_outputs

            invalidate_outputs(writer)
            writer.commit()
        result = pending.result(timeout=5)
        if kind == "answer":
            assert "15.10.2026 в 23:42" in result
        else:
            assert result == ("available" if kind == "read" else True)


@pytest.mark.parametrize("prior", ["read_lock", "write"])
def test_timing_never_queues_after_an_incompatible_prior_operation(db, db_engine, prior):
    from sqlalchemy import text

    from garmin_ai.sleep_timing import acquire_timing_fence

    if prior == "read_lock":
        db.execute(text("SELECT pg_advisory_xact_lock_shared(72104619)"))
    else:
        db.add(AppState(key="synthetic:prior-write", value={}))
        db.flush()
    with db_engine.begin() as reader:
        reader.execute(text("SELECT pg_advisory_xact_lock_shared(72104619)"))
        db.execute(text("SET LOCAL statement_timeout='500ms'"))
        assert not acquire_timing_fence(db)
        assert db.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("kind", ["read", "delivery"])
@pytest.mark.parametrize("cancel", [False, True])
def test_timing_wait_timeout_restores_configuration_and_connection(
    db, db_engine, monkeypatch, kind, cancel
):
    from sqlalchemy import text

    from garmin_ai import sleep_timing

    monkeypatch.setattr(sleep_timing, "TIMING_FENCE_WAIT_MS", 5000 if cancel else 50)
    with db_engine.begin() as writer:
        writer.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        if kind == "read":
            db.execute(text("SET LOCAL lock_timeout='700ms'"))
            if cancel:
                db.execute(text("SET LOCAL statement_timeout='250ms'"))
            assert not sleep_timing.acquire_timing_fence(db)
            assert db.scalar(text("SELECT current_setting('lock_timeout')")) == "700ms"
            assert db.scalar(text("SELECT 1")) == 1
        else:
            with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                previous = connection.scalar(text("SELECT current_setting('lock_timeout')"))
                statement_timeout = connection.scalar(
                    text("SELECT current_setting('statement_timeout')")
                )
                try:
                    connection.execute(text("SET lock_timeout='700ms'"))
                    if cancel:
                        connection.execute(text("SET statement_timeout='250ms'"))
                    assert not sleep_timing.acquire_timing_delivery_fence(connection)
                    assert (
                        connection.scalar(text("SELECT current_setting('lock_timeout')")) == "700ms"
                    )
                    assert connection.scalar(text("SELECT 1")) == 1
                finally:
                    connection.execute(
                        text("SELECT set_config('lock_timeout', :previous, false)"),
                        {"previous": previous},
                    )
                    connection.execute(
                        text("SELECT set_config('statement_timeout', :previous, false)"),
                        {"previous": statement_timeout},
                    )
