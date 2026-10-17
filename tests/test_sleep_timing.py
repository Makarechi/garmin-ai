from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

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
        " " * 16000 + "Когда я заснул сегодня?",
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
        db,
        NoModel(),
        f"Когда я {field} сегодня?",
        Settings(),
        NOW,
        budget=budget,
        update_id=1,
        before_model=lambda: pytest.fail("No external-model transaction boundary"),
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
        "sharing_disabled",
        "provider_unavailable",
        "timezone_changed",
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

    provider = None if case == "provider_unavailable" else Provider()
    response = process_message(db_engine, provider, Settings(telegram_user_id=42), 991)
    assert provider is None or provider.calls == 0
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
    if case == "sharing_disabled":
        from garmin_ai.scenario_packs import ensure_scenario_packs

        ensure_scenario_packs(db)["sleep"].llm_enabled = False
        db.commit()
    if case == "timezone_changed":
        from garmin_ai.models import Person

        db.query(Person).one().timezone = "UTC"
        db.commit()
    sent = []

    class Bot:
        async def send_message(self, **kwargs):
            with db_engine.begin() as probe:
                assert probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104619)"))
                assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104621)"))
                assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104643)"))
            sent.append(kwargs["text"])
            return SimpleNamespace(message_id=991)

    asyncio.run(deliver(Bot(), db_engine, 42, "update:991", response))
    if case in {"source_changed", "generation_and_source_changed", "timezone_changed"}:
        assert sent == [REPLAY_NOTICE]
    elif case in {"forgotten", "goals_changed", "sharing_disabled"}:
        assert sent == []
    else:
        assert len(sent) == 1 and response in sent[0]


@pytest.mark.parametrize("provider", [None, NoModel()], ids=["offline", "online"])
def test_missing_reply_context_needs_no_model(db, tmp_path, provider):
    stored_night(db, tmp_path)
    response = agent.answer_question(
        db, provider, "Когда я заснул сегодня?", Settings(), NOW, reply_to_message_id=999
    )
    assert "Контекст выбранного ответа уже недоступен" in response


def test_web_timing_question_never_initializes_an_unavailable_model(
    db, db_engine, tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from garmin_ai import integrations, web_chat
    from garmin_ai.api import create_app
    from garmin_ai.config import ApiToken, IntegrationInstance

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    stored_night(db, tmp_path)
    db.commit()
    monkeypatch.setattr(web_chat, "datetime", Clock)
    monkeypatch.setattr(
        integrations, "create_model_provider", lambda *args: pytest.fail("Model initialization")
    )
    original = agent.call_tool
    reads = []

    def read(session, *args, **kwargs):
        reads.append(session.info["model_provider_instance_id"])
        return original(session, *args, **kwargs)

    monkeypatch.setattr(agent, "call_tool", read)
    key = "synthetic-sleep-owner-" + "a" * 32
    api = TestClient(
        create_app(
            Settings(
                api_tokens=[ApiToken(key=key, scopes={"admin"})],
                integrations=[
                    IntegrationInstance(id="model:gemini:offline", kind="model", provider="gemini")
                ],
            ),
            db_engine,
        )
    )
    response = api.post(
        "/web-chat/messages",
        json={"client_message_id": str(uuid4()), "text": "Когда я заснул сегодня?"},
        headers={"Authorization": "Bearer " + key},
    )
    assert response.status_code == 200, response.text
    assert "15.10.2026 в 23:42" in response.json()["reply"]["text"]
    assert reads == ["model:gemini:offline"]


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


@pytest.mark.parametrize("provider", [None, NoModel()], ids=["offline", "online"])
def test_disabled_sleep_sharing_still_blocks_direct_answer(db, tmp_path, provider):
    from garmin_ai.scenario_packs import ensure_scenario_packs

    stored_night(db, tmp_path)
    ensure_scenario_packs(db)
    from sqlalchemy import select

    from garmin_ai.models import ModuleConfig

    row = db.scalar(select(ModuleConfig).where(ModuleConfig.pack_key == "sleep"))
    row.llm_enabled = False
    db.flush()
    response = agent.answer_question(db, provider, "Когда я заснул сегодня?", Settings(), NOW)
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


def test_unrelated_replay_generation_keeps_the_timing_snapshot(db, tmp_path, monkeypatch):
    from garmin_ai.normalize import upsert

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
    assert "15.10.2026 в 23:42" in response
    assert db.get(AppState, "analysis:conversation:pending").value["turn"]["answer"] == response


@pytest.mark.parametrize("holder", ["pg_advisory_xact_lock", "pg_advisory_xact_lock_shared"])
def test_timing_answer_reads_while_another_replay_reader_or_writer_is_open(
    db, db_engine, tmp_path, holder
):
    from sqlalchemy import text

    stored_night(db, tmp_path)
    db.commit()
    db.execute(text("SET LOCAL statement_timeout='1500ms'"))
    with db_engine.begin() as connection:
        connection.execute(text(f"SELECT {holder}(72104619)"))
        response = agent.answer_question(
            db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, update_id=1
        )
    assert "15.10.2026 в 23:42" in response
    assert db.get(AppState, "analysis:conversation:pending") is None


def test_overlapping_timing_answers_do_not_upgrade_shared_locks(
    db, db_engine, tmp_path, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    stored_night(db, tmp_path)
    db.commit()
    original = agent.call_tool
    overlapping = []

    def read(session, *args, **kwargs):
        result = original(session, *args, **kwargs)
        if session is db:
            with Session(db_engine) as second:
                second.execute(text("SET LOCAL statement_timeout='1500ms'"))
                overlapping.append(
                    agent.answer_question(
                        second, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, update_id=2
                    )
                )
                assert second.get(AppState, "analysis:conversation:pending") is None
                second.rollback()
        return result

    monkeypatch.setattr(agent, "call_tool", read)
    response = agent.answer_question(
        db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, update_id=1
    )
    assert "15.10.2026 в 23:42" in response
    assert len(overlapping) == 1 and "15.10.2026 в 23:42" in overlapping[0]
    assert db.get(AppState, "analysis:conversation:pending").value["turn"]["update_id"] == "1"


def test_uncommitted_sleep_replacement_does_not_mix_snapshot_evidence(db, db_engine, tmp_path):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    stored_night(db, tmp_path)
    db.commit()
    db.execute(text("SET LOCAL statement_timeout='1500ms'"))
    with Session(db_engine) as writer:
        writer.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        ingest(
            writer,
            LocalArchive(tmp_path / "replacement"),
            "sleep",
            str(DAY),
            {
                "dailySleepDTO": {
                    "calendarDate": str(DAY),
                    "sleepStartTimestampGMT": int((START + timedelta(hours=1)).timestamp() * 1000),
                    "sleepEndTimestampGMT": int((END + timedelta(hours=1)).timestamp() * 1000),
                }
            },
            "Europe/Bratislava",
        )
        result = timing(db)
        assert result["status"] == "available"
        assert datetime.fromisoformat(result["rows"][0]["main_interval"]["start"]) == START
        writer.commit()
    result = timing(db)
    assert result["status"] == "available"
    assert datetime.fromisoformat(result["rows"][0]["main_interval"]["start"]) == START + timedelta(
        hours=1
    )


def test_candidate_identity_change_cannot_mix_a_new_watermark_with_old_times(
    db, db_engine, tmp_path, monkeypatch
):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    stored_night(db, tmp_path)
    db.commit()
    changed = []

    def replace_before_joined_read(conn, cursor, statement, parameters, context, executemany):
        if (
            not changed
            and "JOIN timeline_intervals" in statement
            and "FROM source_payloads" in statement
        ):
            changed.append(True)
            with Session(db_engine) as writer:
                ingest(
                    writer,
                    LocalArchive(tmp_path / "replacement"),
                    "sleep",
                    str(DAY),
                    {
                        "dailySleepDTO": {
                            "calendarDate": str(DAY),
                            "sleepStartTimestampGMT": int(
                                (START + timedelta(hours=1)).timestamp() * 1000
                            ),
                            "sleepEndTimestampGMT": int(
                                (END + timedelta(hours=1)).timestamp() * 1000
                            ),
                        }
                    },
                    "Europe/Bratislava",
                )
                writer.commit()

    event.listen(db_engine, "before_cursor_execute", replace_before_joined_read)
    try:
        result = timing(db)
    finally:
        event.remove(db_engine, "before_cursor_execute", replace_before_joined_read)
    assert changed
    assert result["status"] == "unverified_source"
    assert result["rows"][0]["main_interval"] is None
    assert datetime.fromisoformat(
        timing(db)["rows"][0]["main_interval"]["start"]
    ) == START + timedelta(hours=1)


@pytest.mark.parametrize("provider", [None, NoModel()], ids=["offline", "online"])
def test_literal_question_and_delivery_finish_during_an_open_replay_transaction(
    db, db_engine, tmp_path, monkeypatch, provider
):
    import asyncio
    from contextlib import contextmanager
    from types import SimpleNamespace

    from sqlalchemy import text

    from garmin_ai import proactive, telegram

    stored_night(db, tmp_path)
    for update_id, message in [(989, "/help"), (991, "Когда я заснул сегодня?")]:
        telegram.save_update(
            db,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(NOW.timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": message,
                },
            },
            42,
        )
    db.commit()
    telegram.process_message(db_engine, None, Settings(telegram_user_id=42), 989)
    original = telegram.transaction

    @contextmanager
    def bounded_transaction(engine):
        with original(engine) as session:
            session.execute(text("SET LOCAL statement_timeout='1500ms'"))
            yield session

    monkeypatch.setattr(telegram, "transaction", bounded_transaction)
    monkeypatch.setattr(
        proactive,
        "reconcile_answers",
        lambda *args: pytest.fail("Unrelated question reconciliation"),
    )
    sent = []

    class Bot:
        async def send_message(self, **kwargs):
            with db_engine.begin() as probe:
                assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104621)"))
                assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104643)"))
            sent.append(kwargs["text"])
            return SimpleNamespace(message_id=991)

    with db_engine.begin() as replay_writer:
        replay_writer.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        response = telegram.process_message(db_engine, provider, Settings(telegram_user_id=42), 991)
        assert "15.10.2026 в 23:42" in response
        assert db.get(AppState, "analysis:conversation:pending", populate_existing=True) is None
        asyncio.run(telegram.deliver(Bot(), db_engine, 42, "update:991", response, keyboard=True))
    assert len(sent) == 1 and response in sent[0]


@pytest.mark.parametrize("channel_only", [False, True])
def test_forgetting_waits_for_the_fact_delivery_policy_fence(db, db_engine, channel_only):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import sleep

    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from garmin_ai.conversation import current_epoch, forget_channel_context, forget_conversation

    previous = current_epoch(db)
    db.commit()
    started = Event()
    pids = []

    def forget():
        with Session(db_engine) as session:
            pids.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            if channel_only:
                forget_channel_context(session, "telegram:primary")
            else:
                forget_conversation(session)
            session.commit()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as delivery:
            delivery.execute(text("SELECT pg_advisory_lock_shared(72104621)"))
            delivery.execute(text("SELECT pg_advisory_lock_shared(72104643)"))
            try:
                pending = executor.submit(forget)
                assert started.wait(3)
                for _ in range(200):
                    with db_engine.connect() as probe:
                        waiting = probe.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid "
                                "AND locktype='advisory' AND objid=72104643 AND NOT granted)"
                            ),
                            {"pid": pids[0]},
                        )
                    if waiting:
                        break
                    if pending.done():
                        pending.result()
                        pytest.fail("Forgetting did not fence in-flight fact delivery")
                    sleep(0.01)
                else:
                    pytest.fail("Forgetting never joined the delivery policy fence")
            finally:
                delivery.execute(text("SELECT pg_advisory_unlock_shared(72104643)"))
                delivery.execute(text("SELECT pg_advisory_unlock_shared(72104621)"))
        pending.result(timeout=5)
    assert current_epoch(db) != previous


def test_busy_timing_fact_does_not_write_a_legacy_goal_binding(db, db_engine, tmp_path):
    from sqlalchemy import text

    from garmin_ai.personal_goals import KEY

    stored_night(db, tmp_path)
    db.add(
        AppState(
            key=KEY,
            value={
                "configured": True,
                "revision": 1,
                "goals": ["sleep"],
                "updated_at": NOW.isoformat(),
            },
        )
    )
    db.commit()
    db.execute(text("SET LOCAL statement_timeout='1500ms'"))
    with db_engine.begin() as writer:
        writer.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        response = agent.answer_question(
            db, NoModel(), "Когда я заснул сегодня?", Settings(), NOW, update_id=1
        )
    assert "15.10.2026 в 23:42" in response
    assert "owner_id" not in db.get(AppState, KEY, populate_existing=True).value


def test_onboarding_revocation_serializes_before_timing_delivery(db, db_engine, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import sleep

    from sqlalchemy import text

    from garmin_ai import onboarding
    from garmin_ai.onboarding import OnboardingPlan

    plan = OnboardingPlan(
        locale="ru",
        timezone="Europe/Bratislava",
        units="metric",
        selected_packs={"sleep"},
        model_categories={"health", "diary"},
    )
    onboarding.apply_onboarding(db, plan)
    db.commit()
    start, ready = Event(), Event()
    pids = []

    def publish():
        with db_engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout='3000ms'"))
            pids.append(connection.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            assert start.wait(3)
            connection.execute(text("SELECT pg_advisory_xact_lock_shared(72104621)"))
            connection.execute(text("SELECT pg_advisory_xact_lock_shared(72104643)"))

    original = onboarding.configure_scenario_pack
    observed = []

    def configure(session, key, selection):
        result = original(session, key, selection)
        if key == "sleep" and not selection.llm_enabled:
            start.set()
            assert ready.wait(2)
            for _ in range(200):
                with db_engine.connect() as probe:
                    waiting = probe.scalar(
                        text(
                            "SELECT objid FROM pg_locks WHERE pid=:pid "
                            "AND locktype='advisory' AND NOT granted LIMIT 1"
                        ),
                        {"pid": pids[0]},
                    )
                if waiting is not None:
                    observed.append(waiting)
                    assert waiting == 72104621, (
                        "Delivery entered between forgetting and policy upgrade"
                    )
                    break
                sleep(0.01)
            else:
                pytest.fail("Publisher did not wait for the settings transaction")
        return result

    monkeypatch.setattr(onboarding, "configure_scenario_pack", configure)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(publish)
        try:
            onboarding.apply_onboarding(
                db, plan.model_copy(update={"model_categories": set(), "timezone": "UTC"})
            )
            db.commit()
        finally:
            db.rollback()
            start.set()
        pending.result(timeout=5)
    assert observed == [72104621]


@pytest.mark.parametrize("lock_id", [72104631, 72104621, 72104643, 72104626])
def test_busy_timing_delivery_defers_without_waiting(db, db_engine, tmp_path, monkeypatch, lock_id):
    import asyncio

    from sqlalchemy import text

    from garmin_ai import telegram

    stored_night(db, tmp_path)
    telegram.save_update(
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
    response = telegram.process_message(db_engine, None, Settings(telegram_user_id=42), 991)
    assert "23:42" in response
    # Bound an old blocking implementation so this regression cannot hang.
    from sqlalchemy import event

    def bound_lock(conn, cursor, statement, parameters, context, executemany):
        if (
            "pg_advisory_lock_shared" in statement
            or "pg_advisory_xact_lock_shared(72104626)" in statement
        ):
            cursor.execute("SET statement_timeout='200ms'")

    class Bot:
        async def send_message(self, **kwargs):
            pytest.fail("A busy publication fence must defer the send")

    event.listen(db_engine, "before_cursor_execute", bound_lock)
    try:
        with db_engine.begin() as owner:
            owner.execute(text(f"SELECT pg_advisory_xact_lock({lock_id})"))
            with pytest.raises(telegram.DiaryDeferred):
                asyncio.run(
                    telegram.deliver(Bot(), db_engine, 42, "update:991", response, keyboard=True)
                )
    finally:
        event.remove(db_engine, "before_cursor_execute", bound_lock)
        db_engine.dispose()


@pytest.mark.parametrize("stage", ["first_body", "queued_history", "read_history"])
@pytest.mark.parametrize(
    "change",
    ["unchanged", "pack", "category", "timezone", "source", "forget", "goals", "proof_missing"],
)
def test_web_timing_is_rechecked_for_every_exposure(
    db, db_engine, tmp_path, monkeypatch, stage, change
):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from garmin_ai import integrations, web_chat
    from garmin_ai.api import create_app
    from garmin_ai.config import ApiToken, IntegrationInstance
    from garmin_ai.models import OutboxMessage, Person
    from garmin_ai.normalize import upsert

    clock = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)

    stored_night(db, tmp_path)
    db.commit()
    monkeypatch.setattr(web_chat, "datetime", Clock)
    monkeypatch.setattr(
        integrations, "create_model_provider", lambda *args: pytest.fail("Model initialization")
    )
    key = "synthetic-timing-owner-" + "a" * 32
    api = TestClient(
        create_app(
            Settings(
                api_tokens=[ApiToken(key=key, scopes={"admin"})],
                integrations=[
                    IntegrationInstance(id="model:gemini:offline", kind="model", provider="gemini")
                ],
            ),
            db_engine,
        )
    )
    headers = {"Authorization": "Bearer " + key}
    changed = []

    def mutate():
        changed.append(True)
        with Session(db_engine) as session, session.begin():
            if change == "pack":
                from garmin_ai.scenario_packs import ensure_scenario_packs

                ensure_scenario_packs(session)["sleep"].llm_enabled = False
            elif change == "category":
                upsert(
                    session,
                    AppState,
                    {"key": "preferences:onboarding", "value": {"model_categories": []}},
                    ["key"],
                )
            elif change == "timezone":
                session.scalar(select(Person)).timezone = "UTC"
                upsert(
                    session,
                    AppState,
                    {
                        "key": "preferences:onboarding",
                        "value": {"model_categories": ["health", "diary"]},
                    },
                    ["key"],
                )
            elif change == "source":
                session.get(TimelineInterval, f"sleep:{DAY}").confirmed = False
            elif change == "proof_missing":
                for reference in session.scalar(select(OutboxMessage)).intent["evidence_refs"]:
                    if reference.startswith(web_chat.TIMING_REFERENCE):
                        session.delete(session.get(AppState, reference))
            elif change == "forget":
                from garmin_ai.conversation import forget_channel_context

                forget_channel_context(session, "web:local")
            elif change == "goals":
                from garmin_ai.personal_goals import GoalSelection, select_goals

                select_goals(session, GoalSelection(revision=0, goals=["sleep"]), NOW)

    original = web_chat._fenced_response
    original_allowed = web_chat._intent_allowed

    def allowed(session, value):
        if web_chat._has_timing_reference(value):
            from sqlalchemy import text

            with db_engine.begin() as probe:
                for lock in (72104631, 72104621, 72104643, 72104626):
                    assert not probe.scalar(text(f"SELECT pg_try_advisory_xact_lock({lock})"))
        return original_allowed(session, value)

    monkeypatch.setattr(web_chat, "_intent_allowed", allowed)

    def fenced(engine, body, intents, **kwargs):
        if stage == "first_body" and "reply" in body and not changed:
            mutate()
        return original(engine, body, intents, **kwargs)

    monkeypatch.setattr(web_chat, "_fenced_response", fenced)
    body = {"client_message_id": str(uuid4()), "text": "Когда я заснул сегодня?"}
    response = api.post("/web-chat/messages", json=body, headers=headers)
    assert response.status_code == 200, response.text
    if stage == "first_body" and change != "unchanged":
        assert response.json()["reply"] is None
    else:
        assert "15.10.2026 в 23:42" in response.json()["reply"]["text"]
    if stage != "first_body":
        if stage == "read_history":
            clock[0] = datetime.now(UTC)
            receipt = api.post(
                f"/web-chat/messages/{response.json()['reply']['id']}/read",
                json={},
                headers=headers,
            )
            assert receipt.status_code == 200, receipt.text
        mutate()
    visible = api.get("/web-chat/messages", headers=headers)
    assert visible.status_code == 200, visible.text
    if change == "unchanged":
        assert len(visible.json()["replies"]) == 1
    else:
        assert visible.json()["replies"] == []
        retry = api.post("/web-chat/messages", json=body, headers=headers)
        assert retry.status_code == 200, retry.text
        assert retry.json()["reply"] is None
        db.expire_all()
        outbox = db.scalar(select(OutboxMessage))
        denied = api.post(f"/web-chat/messages/{outbox.id}/read", json={}, headers=headers)
        assert denied.status_code == 409


@pytest.mark.parametrize("phase", ["recovery", "claim"])
def test_neutral_background_work_skips_a_busy_replay_writer(db, db_engine, monkeypatch, phase):
    import asyncio
    from contextlib import contextmanager

    from sqlalchemy import text

    from garmin_ai import runtime

    db.commit()
    original = runtime.transaction

    @contextmanager
    def bounded(engine):
        with original(engine) as session:
            session.execute(text("SET LOCAL statement_timeout='200ms'"))
            yield session

    monkeypatch.setattr(runtime, "transaction", bounded)
    original_policy = runtime.initiative_delivery_fence
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as writer:
        if phase == "recovery":
            writer.execute(text("SELECT pg_advisory_lock(72104619)"))
        else:

            @contextmanager
            def fenced(engine):
                writer.execute(text("SELECT pg_advisory_lock(72104619)"))
                with original_policy(engine):
                    yield

            monkeypatch.setattr(runtime, "initiative_delivery_fence", fenced)
        try:
            asyncio.run(runtime.deliver_neutral_initiatives(db_engine, {}))
        finally:
            writer.execute(text("SELECT pg_advisory_unlock(72104619)"))


def test_neutral_bookkeeping_wait_keeps_the_event_loop_responsive(db, db_engine, monkeypatch):
    import asyncio
    from threading import Event
    from types import SimpleNamespace

    from sqlalchemy import select

    from garmin_ai import initiative_rules, runtime
    from garmin_ai.channels import (
        ChannelInstanceRef,
        DeliveryAttempt,
        DeliveryState,
        OutboundIntent,
        TextBlock,
    )
    from garmin_ai.models import Person

    intent = OutboundIntent(
        owner_id=db.scalar(select(Person.id)),
        conversation_id=uuid4(),
        channel_instance=ChannelInstanceRef(channel="sample", instance_id="one"),
        blocks=[TextBlock(text="synthetic")],
    )
    db.commit()
    lease = SimpleNamespace(intent=intent)
    monkeypatch.setattr(initiative_rules, "claim_due_initiative", lambda *args, **kwargs: lease)
    started, release = Event(), Event()

    def finish(*args):
        started.set()
        assert release.wait(2), "Bookkeeping blocked the event loop"

    monkeypatch.setattr(initiative_rules, "finish_initiative_attempt", finish)

    class Adapter:
        async def deliver(self, intent, *, now):
            return DeliveryAttempt(
                intent_id=intent.intent_id, state=DeliveryState.FAILED, reason="synthetic"
            )

    async def scenario():
        task = asyncio.create_task(
            runtime.deliver_neutral_initiatives(db_engine, {"sample:one": Adapter}, limit=1)
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            release.set()
            if not task.done():
                await task

    asyncio.run(scenario())
