from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from garmin_ai.config import Settings
from garmin_ai.diary_forms import form_time
from garmin_ai.models import Event, TelegramUpdate
from garmin_ai.telegram import handle_button, process_message, save_update


@pytest.mark.parametrize(
    "button, text, kind",
    [
        ("medication", "synthetic; 1 tablet; сейчас", "medication"),
        ("medication", "неизвестно; неизвестно; сейчас", "medication"),
        ("medication", "synthetic; неизвестно; сейчас", "medication"),
        ("note", "synthetic note; сейчас", "note"),
    ],
)
@pytest.mark.parametrize("configured", [False, True])
def test_explicit_offline_forms_preserve_send_time_and_retry(
    db, db_engine, button, text, kind, configured
):
    now = datetime.now(UTC) - timedelta(minutes=3)
    settings = Settings(telegram_user_id=42, timezone="UTC")
    handle_button(db, button, settings, "owner", 10, now)
    sent = now + timedelta(minutes=1)
    save_update(
        db,
        {
            "update_id": 11,
            "message": {
                "message_id": 11,
                "date": sent.isoformat(),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": text,
            },
        },
        42,
    )
    db.commit()

    class UnavailableProvider:
        def structured(self, *args):
            from garmin_ai.llm import ProviderUnavailable

            raise ProviderUnavailable("synthetic outage")

    provider = UnavailableProvider() if configured else None
    first = process_message(db_engine, provider, settings, 11)
    assert process_message(db_engine, provider, settings, 11) == first
    rows = db.scalars(select(Event)).all()
    assert len(rows) == 1 and rows[0].kind == kind
    assert rows[0].start == sent
    if kind == "medication" and "неизвестно" in text:
        assert rows[0].payload["dose"] is None and rows[0].payload["unit"] is None
        assert "доза неизвестна" in first
        if text.startswith("неизвестно;"):
            assert rows[0].payload["name"] is None
            assert "название неизвестно" in first
    assert db.get(TelegramUpdate, 11, populate_existing=True).status == "processed"


@pytest.mark.parametrize(
    "text",
    [
        "synthetic; сейчас",
        "synthetic; 0 mg; сейчас",
        "future",
    ],
)
@pytest.mark.parametrize("configured", [False, True])
def test_invalid_medication_never_infers_dose_or_time(db, db_engine, text, configured):
    settings = Settings(telegram_user_id=42, timezone="UTC")
    now = datetime.now(UTC)
    if text == "future":
        text = "synthetic; 1 mg; " + (now + timedelta(days=1)).isoformat()
    handle_button(db, "medication", settings, "owner", 10, now)
    save_update(
        db,
        {
            "update_id": 11,
            "message": {
                "message_id": 11,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": text,
            },
        },
        42,
    )
    db.commit()

    class UnavailableProvider:
        def structured(self, *args):
            from garmin_ai.llm import ProviderUnavailable

            raise ProviderUnavailable("synthetic outage")

    response = process_message(
        db_engine, UnavailableProvider() if configured else None, settings, 11
    )
    assert "ещё не добавлена" in response
    assert db.scalar(select(Event)) is None


@pytest.mark.parametrize(
    "now, wall",
    [
        (datetime(2026, 10, 25, 5, tzinfo=UTC), "02:30"),
        (datetime(2026, 10, 25, 1, 15, tzinfo=UTC), "02:30"),
        (datetime(2026, 3, 29, 5, tzinfo=UTC), "02:30"),
    ],
)
def test_ambiguous_or_missing_dst_wall_time_requires_offset(now, wall):
    with pytest.raises(ValueError, match="DST"):
        form_time(wall, now, "Europe/Budapest")


def test_clock_time_uses_original_local_day():
    now = datetime(2026, 9, 10, 1, tzinfo=UTC)
    assert form_time("23:00", now, "UTC") == datetime(2026, 9, 9, 23, tzinfo=UTC)


def test_queued_form_defers_without_provider_call(db, db_engine):
    from garmin_ai.models import Job
    from garmin_ai.telegram import DiaryDeferred

    now = datetime.now(UTC)
    settings = Settings(telegram_user_id=42, timezone="UTC")
    handle_button(db, "medication", settings, "owner", 9, now)
    for update_id, text in [(10, "earlier text"), (11, "synthetic; 1 tablet; сейчас")]:
        save_update(
            db,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": now.isoformat(),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": text,
                },
            },
            42,
        )
    db.commit()

    class UnavailableProvider:
        def structured(self, *args, **kwargs):
            from garmin_ai.llm import ProviderUnavailable

            raise ProviderUnavailable("synthetic outage")

    provider = UnavailableProvider()
    for _ in range(2):
        with pytest.raises(DiaryDeferred):
            process_message(db_engine, provider, settings, 11)
    db.expire_all()
    job = db.scalar(select(Job).where(Job.dedup_key == "telegram:11"))
    assert job.payload["safety_checked"] is True
    assert job.payload["form_safety"] == "unavailable"
    assert db.scalar(select(Event)) is None


def test_explicit_note_with_urgent_symptoms_is_screened_without_saving(db, db_engine):
    from garmin_ai.agent import SafetyScreen

    now = datetime.now(UTC)
    settings = Settings(telegram_user_id=42, timezone="UTC")
    handle_button(db, "note", settings, "owner", 10, now)
    save_update(
        db,
        {
            "update_id": 11,
            "message": {
                "message_id": 11,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "внезапная сильная боль в груди; сейчас",
            },
        },
        42,
    )
    db.commit()

    class UrgentProvider:
        def structured(self, instruction, text, schema):
            assert schema is SafetyScreen
            return SafetyScreen(urgent=True)

    response = process_message(db_engine, UrgentProvider(), settings, 11)
    assert "112" in response
    assert db.scalar(select(Event)) is None
