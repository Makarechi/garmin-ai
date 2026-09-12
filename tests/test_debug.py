from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from garmin_ai.config import Settings
from garmin_ai.debug import KEY, enabled, notice_text, queue_error_notice
from garmin_ai.models import AppState, Job
from garmin_ai.telegram import process_message, save_update


@pytest.mark.parametrize("newer_id,newer_date", [(2, 1788782400), (0, 1788868800)])
def test_delayed_enable_cannot_override_newer_disable(db, db_engine, newer_id, newer_date):
    old = incoming("/debug on", 1)
    newer = incoming("/debug off", newer_id)
    newer["message"]["date"] = newer_date
    save_update(db, old, 42)
    save_update(db, newer, 42)
    db.commit()
    settings = Settings(telegram_user_id=42)
    assert "выключена" in process_message(db_engine, None, settings, newer_id)
    assert "выключена" in process_message(db_engine, None, settings, 1)
    assert not enabled(db)


def incoming(text, identifier=1, owner=42):
    return {
        "update_id": identifier,
        "message": {
            "message_id": identifier,
            "date": 1788782400,
            "from": {"id": owner},
            "chat": {"id": owner, "type": "private"},
            "text": text,
        },
    }


def test_debug_works_without_model_and_bypasses_pending_analysis(db, db_engine):
    settings = Settings(telegram_user_id=42)
    save_update(db, incoming("question"), 42)
    save_update(db, incoming("/debug on", 2), 42)
    db.commit()
    job = db.scalar(select(Job).where(Job.payload["update_id"].as_integer() == 2))
    assert job.kind == "telegram_control"
    assert "включена" in process_message(db_engine, None, settings, 2)
    assert enabled(db)
    save_update(db, incoming("/debug off", 3), 42)
    db.commit()
    assert "выключена" in process_message(db_engine, None, settings, 3)
    assert not enabled(db)


def test_debug_requires_owner_and_invalid_argument_does_not_enable(db, db_engine):
    assert not save_update(db, incoming("/debug on", owner=9), 42)
    save_update(db, incoming("/debug invalid"), 42)
    db.commit()
    assert "Используйте" in process_message(db_engine, None, Settings(telegram_user_id=42), 1)
    assert not enabled(db)


def test_error_notices_opt_in_deduplicate_and_do_not_recurse(db):
    now = datetime(2026, 9, 11, 12, tzinfo=UTC)
    queue_error_notice(db, "telegram_update", "ProviderUnavailable", now)
    assert not db.scalars(select(Job)).all()
    db.add(AppState(key=KEY, value={"enabled": True}))
    db.flush()
    for _ in range(3):
        queue_error_notice(db, "telegram_update", "ProviderUnavailable", now)
    queue_error_notice(db, "telegram_debug_notice", "TimedOut", now)
    queue_error_notice(db, "telegram_update", "DiaryDeferred", now)
    assert len(db.scalars(select(Job)).all()) == 1
    queue_error_notice(db, "telegram_update", "ProviderUnavailable", now + timedelta(minutes=10))
    assert len(db.scalars(select(Job)).all()) == 2


def test_untrusted_error_details_cannot_reach_chat(db):
    db.add(AppState(key=KEY, value={"enabled": True}))
    db.flush()
    queue_error_notice(db, "telegram_poll", "private-token-or-health-text")
    job = db.scalar(select(Job))
    assert job.payload == {"kind": "telegram_poll", "error": "internal"}
    assert "private" not in notice_text(job.payload)
    assert "private" not in notice_text({"kind": "private", "error": "private"})


def test_debug_notices_have_explicit_metrics_label(db):
    from garmin_ai.observability import prometheus

    db.add(AppState(key=KEY, value={"enabled": True}))
    db.flush()
    queue_error_notice(db, "telegram_poll", "TimedOut")
    metrics = prometheus(db)
    assert 'kind="telegram_debug_notice"' in metrics
    assert 'kind="other"' not in metrics
