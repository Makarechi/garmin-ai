"""Opt-in provider contract checks using synthetic diary data only."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from garmin_ai.agent import interpret
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.llm import GeminiProvider

pytestmark = pytest.mark.skipif(
    os.environ.get("GA_LIVE_GEMINI_TESTS") != "1", reason="Explicit live Gemini opt-in required"
)


@pytest.mark.parametrize(
    "message,intent,kind",
    [
        ("кофе был в 11", "log", "caffeine"),
        ("два эспрессо после обеда", "clarify", None),
        ("мигрень началась часа два назад, 6 из 10", "log", "migraine"),
        ("таблетку выпил через 20 минут", "clarify", None),
        ("отмени последнюю запись", "undo", None),
        ("кофе в 11 и мигрень в 13, боль 6 из 10", "log", "caffeine"),
    ],
)
def test_live_russian_diary(db, message, intent, kind):
    settings = Settings()
    provider = GeminiProvider(settings)
    try:
        result = interpret(db, provider, message, settings, datetime(2026, 9, 7, 18, tzinfo=UTC))
        assert result.intent == intent
        if kind:
            assert result.events[0].payload.type == kind
        if "два часа" in message or "часа два" in message:
            assert result.events[0].start == datetime(2026, 9, 7, 16, tzinfo=UTC)
        if "кофе в 11 и" in message:
            assert len(result.events) == 2
    finally:
        provider.close()


def test_live_close_and_correction(db):
    row = create_event(
        db,
        EventInput(
            start="2026-09-07T12:00:00+02:00",
            payload={"type": "migraine", "severity": 6, "aura": False},
        ),
        actor="synthetic",
    )
    settings = Settings()
    provider = GeminiProvider(settings)
    try:
        now = datetime(2026, 9, 7, 18, tzinfo=UTC)
        closed = interpret(db, provider, "закончилась в 18:30", settings, now)
        assert closed.intent == "close" and closed.target_event_id == row.id
        assert closed.events[0].end == datetime(2026, 9, 7, 16, 30, tzinfo=UTC)
        corrected = interpret(db, provider, "исправь силу боли на 4 из 10", settings, now)
        assert corrected.intent == "update" and corrected.changed_fields == ["payload.severity"]
    finally:
        provider.close()


def test_live_voice_transcription():
    path = Path("data/validation/synthetic-voice.wav")
    if not path.exists():
        pytest.skip("Synthetic speech fixture must be generated locally")
    provider = GeminiProvider(Settings())
    try:
        transcript = provider.transcribe(path.read_bytes(), "audio/wav")
        assert "кофе" in transcript.lower()
        assert "11" in transcript or "одиннадцать" in transcript.lower()
    finally:
        provider.close()


def test_live_button_refinement_changes_existing_episode(db):
    from sqlalchemy import func, select

    from garmin_ai.agent import apply_command
    from garmin_ai.models import Event
    from garmin_ai.telegram import handle_button

    now = datetime(2026, 9, 7, 18, tzinfo=UTC)
    settings = Settings()
    for index in range(14):
        create_event(
            db,
            EventInput(start=now, payload={"type": "note", "description": f"synthetic {index}"}),
            actor="synthetic",
        )
    handle_button(db, "migraine", settings, "synthetic", 700, now)
    provider = GeminiProvider(settings)
    try:
        command = interpret(db, provider, "7, без ауры", settings, now)
        assert command.intent == "update"
        apply_command(db, command, text="7, без ауры", update_id=701, actor="synthetic", now=now)
        db.flush()
        row = db.scalar(select(Event).where(Event.kind == "migraine"))
        assert row.payload["severity"] == 7 and row.payload["aura"] is False
        assert db.scalar(select(func.count()).select_from(Event)) == 15
    finally:
        provider.close()


def test_live_urgent_report_bypasses_stalled_diary(db, db_engine):
    from datetime import timedelta

    from sqlalchemy import func, select

    from garmin_ai.models import Event, Job, TelegramUpdate
    from garmin_ai.telegram import process_message, save_update

    now = datetime.now(UTC)
    settings = Settings(telegram_user_id=42)
    for identity, text in [
        (1, "кофе в 11"),
        (2, "Внезапные тяжёлые симптомы, мне нужна срочная медицинская помощь прямо сейчас"),
    ]:
        save_update(
            db,
            {
                "update_id": identity,
                "message": {
                    "message_id": identity,
                    "date": int(now.timestamp()),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": text,
                },
            },
            42,
        )
    db.scalar(select(Job).where(Job.dedup_key == "telegram:1")).run_at = now + timedelta(hours=1)
    db.commit()
    provider = GeminiProvider(settings)
    try:
        answer = process_message(db_engine, provider, settings, 2)
        assert "112" in answer
        db.expire_all()
        assert db.get(TelegramUpdate, 1).status == "pending"
        assert db.get(TelegramUpdate, 2).status == "processed"
        assert db.scalar(select(func.count()).select_from(Event)) == 0
    finally:
        provider.close()


def test_live_close_selects_bounded_candidates_in_large_diary(db):
    from sqlalchemy import func, select

    from garmin_ai.agent import apply_command
    from garmin_ai.models import Event
    from garmin_ai.telegram import handle_button

    now = datetime(2026, 9, 7, 18, tzinfo=UTC)
    first = create_event(
        db,
        EventInput(start="2026-09-07T10:00:00+02:00", payload={"type": "migraine"}),
        actor="synthetic",
    )
    second = create_event(
        db,
        EventInput(start="2026-09-07T12:00:00+02:00", payload={"type": "migraine"}),
        actor="synthetic",
    )
    for index in range(14):
        create_event(
            db,
            EventInput(start=now, payload={"type": "note", "description": f"synthetic {index}"}),
            actor="synthetic",
        )
    settings = Settings()
    handle_button(db, "end", settings, "synthetic", 800, now, time_known=False)
    provider = GeminiProvider(settings)
    try:
        text = "Та мигрень, которая началась сегодня в 12:00, закончилась сегодня в 18:30."
        command = interpret(db, provider, text, settings, now)
        assert command.intent == "close" and command.target_event_id == second.id
        apply_command(db, command, text=text, update_id=801, actor="synthetic", now=now)
        db.flush()
        assert first.end is None and second.end == datetime(2026, 9, 7, 16, 30, tzinfo=UTC)
        assert db.scalar(select(func.count()).select_from(Event)) == 16
    finally:
        provider.close()


def test_live_oversized_transcript_detects_urgent_report(db):
    text = "Это вымышленный обычный дневник: кофе, прогулка и чтение. " * 360
    text += " Сейчас внезапно появились тяжёлые опасные симптомы, мне нужна срочная медицинская помощь прямо сейчас. "
    assert len(text) > 16000
    provider = GeminiProvider(Settings())
    try:
        command = interpret(
            db, provider, text, Settings(), datetime.now(UTC), source="telegram_voice"
        )
        assert command.intent == "safety" and command.confidence == 1
        assert not command.events
    finally:
        provider.close()


@pytest.mark.parametrize(
    "message,start",
    [
        ("Когда я заснул сегодня?", "2026-10-15T21:42:00+00:00"),
        ("Когда я заснул сегодня?", "2026-10-15T22:17:00+00:00"),
        ("Во сколько я вчера вечером лёг спать?", "2026-10-15T21:42:00+00:00"),
        ("Когда я проснулся сегодня?", "2026-10-15T21:42:00+00:00"),
    ],
)
def test_live_sleep_time_uses_one_night_and_verified_timestamp(db, tmp_path, message, start):
    import json

    from garmin_ai.agent import AnalysisBudget, answer_question, simple_sleep_time_field
    from garmin_ai.archive import LocalArchive
    from garmin_ai.ingest import ingest

    end = "2026-10-16T05:30:00+00:00"
    ingest(
        db,
        LocalArchive(tmp_path),
        "sleep",
        "2026-10-16",
        {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": int(datetime.fromisoformat(start).timestamp() * 1000),
                "sleepEndTimestampGMT": int(datetime.fromisoformat(end).timestamp() * 1000),
            }
        },
        "Europe/Bratislava",
    )
    settings = Settings()
    delegate = GeminiProvider(settings)

    class RecordingProvider:
        instance_id = delegate.instance_id
        steps = []

        def structured(self, *args):
            step = delegate.structured(*args)
            self.steps.append(step)
            return step

    provider = RecordingProvider()
    budget = AnalysisBudget()
    try:
        now = datetime(2026, 10, 16, 7, tzinfo=UTC)
        command = interpret(
            db, provider, message, settings, now, source="telegram_text", budget=budget
        )
        assert command.intent == "question"
        response = answer_question(db, provider, message, settings, now, budget=budget)
        calls = [call for step in provider.steps for call in getattr(step, "calls", [])]
        if simple_sleep_time_field(message) is not None:
            assert not calls and budget.model_calls == 0
        else:
            assert len(calls) == 1 and calls[0].name == "analysis_sleep"
            arguments = json.loads(calls[0].arguments_json)
            assert arguments["start"] == arguments["end"] == "2026-10-16"
            assert arguments.get("nap_policy", "separate") == "separate"
            assert provider.steps[-1].timestamp_claims
            assert budget.model_calls == 3
        expected = (
            "16.10.2026 в 07:30"
            if "проснулся" in message
            else "15.10.2026 в 23:42"
            if start.endswith("21:42:00+00:00")
            else "16.10.2026 в 00:17"
        )
        assert expected in response
    finally:
        delegate.close()
