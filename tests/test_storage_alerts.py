from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from garmin_ai.config import Settings
from garmin_ai.models import AppState, Job
from garmin_ai.storage_alerts import KEY, check_storage, current_shortage, schedule_storage_check

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def config():
    return Settings(backup_key="synthetic", telegram_user_id=42, telegram_bot_token="synthetic")


def test_capacity_checks_are_bounded_and_disabled_without_backups(db):
    schedule_storage_check(db, Settings(backup_key=""), NOW)
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    for minutes in (0, 1, 30):
        schedule_storage_check(db, config(), NOW + timedelta(minutes=minutes))
    assert db.scalar(select(func.count()).select_from(Job)) == 1
    schedule_storage_check(db, config(), NOW + timedelta(hours=6))
    assert db.scalar(select(func.count()).select_from(Job)) == 2


def test_shortage_notice_is_durable_deduplicated_and_stops_after_recovery(
    db, db_engine, monkeypatch
):
    from garmin_ai import storage_alerts

    status = ["insufficient"]
    monkeypatch.setattr(
        storage_alerts,
        "backup_space",
        lambda *args: {"status": status[0], "estimate_only": True, "volumes": []},
    )
    for hours in (0, 6, 12):
        check_storage(db_engine, config(), NOW + timedelta(hours=hours))
    db.expire_all()
    assert current_shortage(db)
    assert db.scalar(select(func.count()).select_from(Job)) == 1
    assert db.scalar(select(Job)).payload == {"day": "2026-09-10"}
    assert "synthetic" not in str(db.get(AppState, KEY).value)
    db.commit()
    status[0] = "ready"
    check_storage(db_engine, config(), NOW + timedelta(hours=18))
    db.expire_all()
    assert not current_shortage(db)


def test_no_telegram_credentials_means_metrics_only(db, db_engine, monkeypatch):
    from garmin_ai import storage_alerts

    monkeypatch.setattr(storage_alerts, "backup_space", lambda *args: {"status": "insufficient"})
    check_storage(db_engine, Settings(backup_key="synthetic", telegram_bot_token=""), NOW)
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert current_shortage(db)


def test_notice_delivery_is_idempotent_and_discards_old_offline_buckets(db, db_engine):
    import asyncio
    from types import SimpleNamespace

    from garmin_ai.storage_alerts import NOTICE, deliver_storage_notice

    db.add(AppState(key=KEY, value={"status": "insufficient"}))
    db.commit()
    messages = []

    class Bot:
        async def send_message(self, **kwargs):
            messages.append(kwargs["text"])
            return SimpleNamespace(message_id=1)

    async def run():
        await deliver_storage_notice(Bot(), db_engine, config(), {"day": "2026-09-09"}, NOW)
        for _ in range(2):
            await deliver_storage_notice(Bot(), db_engine, config(), {"day": "2026-09-10"}, NOW)

    asyncio.run(run())
    assert messages == [NOTICE]
