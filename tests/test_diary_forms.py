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
        ("note", "synthetic note; сейчас", "note"),
    ],
)
def test_explicit_offline_forms_preserve_send_time_and_retry(db, db_engine, button, text, kind):
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
    first = process_message(db_engine, None, settings, 11)
    assert process_message(db_engine, None, settings, 11) == first
    rows = db.scalars(select(Event)).all()
    assert len(rows) == 1 and rows[0].kind == kind
    assert rows[0].start == sent
    assert db.get(TelegramUpdate, 11, populate_existing=True).status == "processed"


@pytest.mark.parametrize(
    "text",
    [
        "synthetic; сейчас",
        "synthetic; неизвестно; сейчас",
        "synthetic; 0 mg; сейчас",
        "synthetic; 1 mg; 2030-01-01T12:00+00:00",
    ],
)
def test_invalid_medication_never_infers_dose_or_time(db, db_engine, text):
    settings = Settings(telegram_user_id=42, timezone="UTC")
    now = datetime.now(UTC)
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
    response = process_message(db_engine, None, settings, 11)
    assert "ещё не добавлена" in response
    assert db.scalar(select(Event)) is None


@pytest.mark.parametrize(
    "now, wall",
    [
        (datetime(2026, 10, 25, 5, tzinfo=UTC), "02:30"),
        (datetime(2026, 3, 29, 5, tzinfo=UTC), "02:30"),
    ],
)
def test_ambiguous_or_missing_dst_wall_time_requires_offset(now, wall):
    with pytest.raises(ValueError, match="DST"):
        form_time(wall, now, "Europe/Budapest")


def test_clock_time_uses_original_local_day():
    now = datetime(2026, 9, 10, 1, tzinfo=UTC)
    assert form_time("23:00", now, "UTC") == datetime(2026, 9, 9, 23, tzinfo=UTC)
