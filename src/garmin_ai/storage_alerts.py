"""Periodic sanitized backup capacity state and durable technical notices."""

from datetime import UTC, datetime

from sqlalchemy import select

from garmin_ai.backup_space import backup_space
from garmin_ai.db import transaction
from garmin_ai.jobs import enqueue
from garmin_ai.models import AppState, Job
from garmin_ai.normalize import upsert

KEY = "storage:backup-capacity"
NOTICE = "Мало свободного места для следующей резервной копии. Проверьте хранилище и запустите backup-space. Последняя копия не удалялась автоматически."


def schedule_storage_check(session, settings, now):
    if settings.backup_key.get_secret_value():
        enqueue(session, "storage_check", {}, f"storage-check:{int(now.timestamp()) // 21600}", now)


def check_storage(engine, settings, now=None):
    now = now or datetime.now(UTC)
    report = backup_space(engine, settings, settings.backup_dir / "capacity-check.enc")
    with transaction(engine) as session:
        upsert(session, AppState, {"key": KEY, "value": {"at": now.isoformat(), **report}}, ["key"])
        if (
            report["status"] == "insufficient"
            and settings.telegram_user_id
            and settings.telegram_bot_token.get_secret_value()
        ):
            day = now.astimezone(UTC).date().isoformat()
            key = f"storage-notice:{day}"
            enqueue(session, "telegram_storage_notice", {"day": day}, key, now)
            receipt = session.get(AppState, f"outbox:{key}:0")
            job = session.scalar(select(Job).where(Job.dedup_key == key).with_for_update())
            if job and job.status == "done" and receipt is None:
                job.status, job.run_at, job.attempts = "pending", now, 0
                job.completed_at, job.last_error = None, None
    return report


def current_shortage(session):
    row = session.get(AppState, KEY)
    return bool(row and row.value.get("status") == "insufficient")


async def deliver_storage_notice(bot, engine, settings, payload, now=None):
    from garmin_ai.telegram import deliver

    now = now or datetime.now(UTC)
    if payload.get("day") != now.astimezone(UTC).date().isoformat():
        return  # An offline period must not release a backlog of obsolete warnings.
    with transaction(engine) as session:
        shortage = current_shortage(session)
    if shortage:
        await deliver(
            bot,
            engine,
            settings.telegram_user_id,
            f"storage-notice:{payload['day']}",
            NOTICE,
            keyboard=False,
        )
