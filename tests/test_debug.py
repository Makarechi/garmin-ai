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
    assert job.payload == {"kind": "telegram_poll", "error": "internal", "generation": [None, None]}
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
    assert 'garmin_ai_queue_due_count{lane="telegram"} 1' in metrics


@pytest.mark.parametrize(
    "error,label",
    [
        ("NetworkError", "Telegram"),
        ("GarminConnectTooManyRequestsError", "лимит запросов Garmin"),
        ("GarminConnectConnectionError", "соединения с Garmin"),
        ("CircuitOpen", "временно приостановлено"),
    ],
)
def test_expected_transport_errors_have_public_labels(db, error, label):
    db.add(AppState(key=KEY, value={"enabled": True}))
    db.flush()
    queue_error_notice(db, "garmin_endpoint", error)
    job = db.scalar(select(Job))
    assert label in notice_text(job.payload)
    assert "внутренняя ошибка" not in notice_text(job.payload)


@pytest.mark.parametrize("control_status", ["pending", "running"])
def test_debug_backlog_waits_for_opt_out_controls(db, db_engine, control_status):
    from garmin_ai.jobs import claim

    now = datetime.now(UTC)
    db.add(AppState(key=KEY, value={"enabled": True}))
    db.flush()
    queue_error_notice(db, "telegram_poll", "NetworkError", now - timedelta(hours=2))
    save_update(db, incoming("/debug off", 901), 42)
    db.flush()
    control = db.scalar(select(Job).where(Job.kind == "telegram_control"))
    control.status = control_status
    control.run_at = now + timedelta(minutes=1)
    control.lease_until = now + timedelta(minutes=1) if control_status == "running" else None
    db.commit()
    assert claim(db, kinds=["telegram_debug_notice"], now=now) is None
    db.commit()
    process_message(db_engine, None, Settings(telegram_user_id=42), 901)
    control.status = "done"
    db.commit()
    assert claim(db, kinds=["telegram_debug_notice"], now=now) is not None
    assert not enabled(db)


def test_reenabled_debug_never_resurrects_previous_opt_in_notices(db, db_engine):
    from garmin_ai.debug import can_deliver

    settings = Settings(telegram_user_id=42)
    save_update(db, incoming("/debug on", 101), 42)
    db.commit()
    process_message(db_engine, None, settings, 101)
    now = datetime.now(UTC)
    queue_error_notice(db, "telegram_poll", "NetworkError", now)
    db.flush()
    old = db.scalar(select(Job).where(Job.kind == "telegram_debug_notice"))
    assert can_deliver(db, old.payload)
    for identity, command in [(102, "/debug off"), (103, "/debug on")]:
        save_update(db, incoming(command, identity), 42)
        db.commit()
        process_message(db_engine, None, settings, identity)
    db.expire_all()
    assert enabled(db) and not can_deliver(db, old.payload)
    queue_error_notice(db, "telegram_poll", "NetworkError", now)
    db.flush()
    notices = db.scalars(select(Job).where(Job.kind == "telegram_debug_notice")).all()
    assert len(notices) == 2
    assert sum(can_deliver(db, job.payload) for job in notices) == 1
    assert not can_deliver(db, {"kind": "telegram_poll", "error": "NetworkError"})


def test_applied_control_delivery_backoff_does_not_block_debug_notices(db, db_engine):
    from garmin_ai.jobs import claim

    save_update(db, incoming("/debug on", 801), 42)
    db.commit()
    process_message(db_engine, None, Settings(telegram_user_id=42), 801)
    now = datetime.now(UTC)
    control = db.scalar(select(Job).where(Job.kind == "telegram_control"))
    control.run_at = now + timedelta(hours=1)
    queue_error_notice(db, "telegram_control", "NetworkError", now)
    db.commit()
    assert (
        claim(db, kinds=["telegram_control", "telegram_debug_notice"], now=now).kind
        == "telegram_debug_notice"
    )
