"""Single-host service supervisor with independent Garmin and Telegram lanes."""

import asyncio
import json
import logging
import signal
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from telegram import Bot
from telegram.error import BadRequest, RetryAfter

from garmin_ai.accounts import AccountError
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.garmin import AuthenticationRequired, GarminReader
from garmin_ai.jobs import claim, enqueue, finish, renew, schedule_backup
from garmin_ai.llm import (
    GeminiProvider,
    ProviderConsentRequired,
    ProviderRateLimited,
    ProviderUnavailable,
)
from garmin_ai.models import AppState, Insight, Job, PendingQuestion, TelegramUpdate
from garmin_ai.normalize import upsert
from garmin_ai.operations import scheduled_backup
from garmin_ai.proactive import (
    can_notify,
    generate_insights,
    generate_questions,
    pending_insight_notices,
    reconcile_questions,
    reserve_insight_notice,
    select_question,
)
from garmin_ai.sync import run_garmin_job, schedule_sync
from garmin_ai.telegram import (
    DeliveryUncertain,
    DiaryDeferred,
    deliver,
    owned_message,
    poll,
    process_message,
    reconcile_failed_inbox,
)


async def run_blocking(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling an await does not stop a native thread. Keep resources held.
        try:
            await task
        except Exception:
            pass
        raise


async def drain_workers(tasks):
    # SIGTERM drains current jobs. A supervisor may kill the whole process if needed.
    await asyncio.gather(*tasks, return_exceptions=True)


class VoiceTooLarge(ValueError):
    pass


async def transcribe_voice(bot, provider, voice):
    if voice.get("duration", 0) > 600 or voice.get("file_size", 0) > 20 * 1024 * 1024:
        raise VoiceTooLarge()
    file = await bot.get_file(voice["file_id"])
    data = bytes(await file.download_as_bytearray())
    if len(data) > 20 * 1024 * 1024:
        raise VoiceTooLarge()
    return await run_blocking(provider.transcribe, data, voice.get("mime_type") or "audio/ogg")


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "time": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                **{
                    k: getattr(record, k)
                    for k in ("job_id", "kind", "error_type")
                    if hasattr(record, k)
                },
            }
        )


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    logger = logging.getLogger("garmin_ai")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for name in ("httpx", "httpcore", "google", "telegram", "garminconnect"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def backup_job_date(job):
    from datetime import date

    value = job.payload.get("date")
    if value is None and job.dedup_key.startswith("backup:"):
        try:
            value = date.fromisoformat(job.dedup_key.removeprefix("backup:")).isoformat()
        except ValueError:
            pass
    return date.fromisoformat(value) if value else job.run_at.date()


def enqueue_connection_notice(session, exc, now):
    category = "account-binding" if isinstance(exc, AccountError) else "auth"
    key = f"{category}:{now.date()}"
    return enqueue(
        session, "telegram_connection_notice", {"category": category, "key": key}, key, now
    )


async def deliver_connection_notice(bot, engine, user_id, payload):
    category = payload["category"]
    message = (
        "Синхронизация Garmin остановлена: владелец аккаунта не подтверждён или не совпадает с владельцем базы. История и дневник доступны. Проверьте исходный аккаунт; для другого владельца нужен отдельный экземпляр. Для старой базы без привязки используйте локальный enroll-account --confirm-existing-owner."
        if category == "account-binding"
        else "Garmin требует повторного входа. История и дневник доступны. Остановите процесс garmin-ai worker (Ctrl+C в его терминале или через диспетчер служб), выполните uv run garmin-ai login и запустите worker тем же способом. Если используете Compose с сервисом worker: docker compose stop worker → uv run garmin-ai login → docker compose start worker."
    )
    await deliver(bot, engine, user_id, payload["key"], message)


async def run(settings: Settings | None = None):
    from garmin_ai.storage_files import exclusive_files

    settings = settings or Settings()
    with exclusive_files(settings):
        await _run(settings)


async def _run(settings):
    setup_logging()
    logger = logging.getLogger("garmin_ai")
    engine = make_engine(settings)
    singleton = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    if not singleton.scalar(text("SELECT pg_try_advisory_lock(72104620)")):
        singleton.close()
        raise RuntimeError("Another Garmin AI runtime is already running")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    archive = LocalArchive(settings.data_dir / "raw")
    reader = None
    try:
        provider = GeminiProvider(settings)
    except ProviderUnavailable:
        provider = None
    bot = (
        Bot(settings.telegram_bot_token.get_secret_value())
        if settings.telegram_bot_token.get_secret_value() and settings.telegram_user_id
        else None
    )

    bot_ready = asyncio.Event()
    notifications_ready = asyncio.Event()

    async def maintain_lease(job_id, token, finished):
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=45)
            except TimeoutError:
                with transaction(engine) as session:
                    if not renew(session, job_id, token):
                        logger.error("lease_lost", extra={"job_id": str(job_id)})
                        stop.set()
                        return

    def garmin_job(kind, payload):
        nonlocal reader
        from garmin_ai.integration import guarded, transport_succeeded

        def operation():
            nonlocal reader
            if reader is None:
                reader = GarminReader.restore(settings.token_dir)
                reader.on_success = lambda: transport_succeeded(engine)
            run_garmin_job(engine, reader, archive, settings, kind, payload)

        try:
            guarded(engine, operation)
        except AuthenticationRequired:
            reader = None
            raise

    async def dispatch(job):
        if job.kind.startswith("garmin_"):
            await run_blocking(garmin_job, job.kind, job.payload)
        elif job.kind == "backup":
            now = datetime.now(UTC)
            destination = settings.backup_dir / f"garmin-ai-{backup_job_date(job)}.enc"
            completed_at = await run_blocking(scheduled_backup, engine, settings, destination)
            with transaction(engine) as session:
                upsert(
                    session,
                    AppState,
                    dict(
                        key="backup:last_success",
                        value={"at": completed_at.isoformat(), "path": str(destination)},
                    ),
                    ["key"],
                )
        elif job.kind == "telegram_connection_notice":
            await deliver_connection_notice(bot, engine, settings.telegram_user_id, job.payload)
        elif job.kind == "telegram_failure":
            if bot is None:
                raise RuntimeError("Telegram is not configured")
            await deliver(
                bot,
                engine,
                settings.telegram_user_id,
                f"failure:{job.payload['update_id']}",
                "Не удалось обработать сообщение после повторных попыток. Пришлите его заново или воспользуйтесь кнопками и /help.",
            )
        elif job.kind == "telegram_ack":
            if bot is None:
                raise RuntimeError("Telegram is not configured")
            with transaction(engine) as session:
                update = session.get(TelegramUpdate, job.payload["update_id"]).payload
            if owned_message(update, settings.telegram_user_id) is None:
                raise ValueError("Unauthorized Telegram update")
            try:
                await bot.answer_callback_query(update["callback_query"]["id"])
            except BadRequest:
                pass  # Expired/already answered callbacks need no retry.
        elif job.kind in {"telegram_update", "telegram_control"}:
            if bot is None:
                raise RuntimeError("Telegram is not configured")
            with transaction(engine) as session:
                update = session.get(TelegramUpdate, job.payload["update_id"]).payload
                cached_reply = session.get(AppState, f"telegram:reply:{job.payload['update_id']}")
                has_reply = cached_reply is not None
            message = owned_message(update, settings.telegram_user_id)
            if message is None:
                raise ValueError("Unauthorized Telegram update")
            message_provider = provider
            transcript = None
            if message.get("voice") and not has_reply:
                voice = message["voice"]
                if provider is None:
                    transcript = ""
                else:
                    try:
                        transcript = await cached_transcription(
                            engine, bot, provider, voice, job.payload["update_id"]
                        )
                    except ProviderConsentRequired:
                        message_provider = None
                        transcript = ""
                    except VoiceTooLarge:
                        await deliver(
                            bot,
                            engine,
                            settings.telegram_user_id,
                            f"update:{job.payload['update_id']}",
                            "Голосовое сообщение слишком большое. Пришлите запись до 10 минут и 20 МБ или напишите текст.",
                        )
                        with transaction(engine) as session:
                            session.get(TelegramUpdate, job.payload["update_id"]).status = "invalid"
                        return
            response = await run_blocking(
                process_message,
                engine,
                message_provider,
                settings,
                job.payload["update_id"],
                transcript,
            )
            with transaction(engine) as session:
                saved_reply = session.get(AppState, f"telegram:reply:{job.payload['update_id']}")
                reply_keyboard = saved_reply.value.get("keyboard", True) if saved_reply else True
            await deliver(
                bot,
                engine,
                settings.telegram_user_id,
                f"update:{job.payload['update_id']}",
                response,
                keyboard=reply_keyboard,
            )
        elif job.kind == "agent_proactive":
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as reservation:
                if not reservation.scalar(text("SELECT pg_try_advisory_lock(72104619)")):
                    raise DiaryDeferred("Diary update in progress")
                try:
                    with transaction(engine) as session:
                        now = datetime.now(UTC)
                        reconcile_questions(session)
                        allow_context = not job.payload.get("context_sync_failures")
                        generate_questions(session, settings, now, allow_context=allow_context)
                        question = (
                            select_question(session, settings, now, allow_context=allow_context)
                            if notifications_ready.is_set() and provider
                            else None
                        )
                    if question:
                        try:
                            await deliver(
                                bot,
                                engine,
                                settings.telegram_user_id,
                                f"question:{question.id}",
                                question.text,
                            )
                        except DeliveryUncertain:
                            with transaction(engine) as session:
                                session.get(PendingQuestion, question.id).status = "uncertain"
                            raise
                        with transaction(engine) as session:
                            session.get(PendingQuestion, question.id).status = "sent"
                finally:
                    reservation.execute(text("SELECT pg_advisory_unlock(72104619)"))
            if (
                not allow_context
                and not job.payload.get("garmin_paused")
                and datetime.now(UTC) < datetime.fromisoformat(job.payload["context_expires_at"])
            ):
                raise DiaryDeferred("Context generation awaits recovered synchronization")
        elif job.kind == "agent_insights":
            with transaction(engine) as session:
                from garmin_ai.integration import paused

                if job.payload.get("garmin_paused") or paused(session, datetime.now(UTC)):
                    # Consume this scheduled cycle without a claim from stale
                    # Garmin evidence; a later cycle resumes after recovery.
                    return
                generate_insights(session, datetime.now(UTC), settings.timezone)
                accepted = pending_insight_notices(session, datetime.now(UTC))
            with transaction(engine) as session:
                allowed = can_notify(session, settings, datetime.now(UTC), include_budget=False)
            if notifications_ready.is_set() and allowed:
                for insight in accepted:
                    metric = insight.dedup_key.split(":")[1]
                    with transaction(engine) as session:
                        if not reserve_insight_notice(
                            session, settings, datetime.now(UTC), insight
                        ):
                            continue
                    try:
                        await deliver(
                            bot,
                            engine,
                            settings.telegram_user_id,
                            f"insight:{insight.id}",
                            insight.statement,
                        )
                    except DeliveryUncertain:
                        with transaction(engine) as session:
                            session.get(Insight, insight.id).status = "uncertain"
                            upsert(
                                session,
                                AppState,
                                dict(
                                    key=f"insight:last:{metric}",
                                    value={"at": datetime.now(UTC).isoformat()},
                                ),
                                ["key"],
                            )
                        continue
                    with transaction(engine) as session:
                        session.get(Insight, insight.id).status = "delivered"
                        upsert(
                            session,
                            AppState,
                            dict(
                                key=f"insight:last:{metric}",
                                value={"at": datetime.now(UTC).isoformat()},
                            ),
                            ["key"],
                        )
        else:
            raise ValueError("Unknown job kind")

    async def worker(kinds):
        while not stop.is_set():
            available = [
                kind
                for kind in kinds
                if (not kind.startswith("telegram_") or bot_ready.is_set())
                and (
                    not bot
                    or kind not in {"agent_proactive", "agent_insights"}
                    or notifications_ready.is_set()
                )
            ]
            with transaction(engine) as session:
                if (
                    bot
                    and session.scalar(
                        select(TelegramUpdate.id).where(TelegramUpdate.status == "pending").limit(1)
                    )
                    is not None
                ):
                    available = [
                        kind
                        for kind in available
                        if kind not in {"agent_proactive", "agent_insights"}
                    ]
                job = (
                    claim(
                        session,
                        kinds=available,
                        backups_enabled=bool(settings.backup_key.get_secret_value()),
                    )
                    if available
                    else None
                )
            if job is None:
                await asyncio.sleep(1)
                continue
            done = asyncio.Event()
            lease_task = asyncio.create_task(maintain_lease(job.id, job.lease_token, done))
            error = None
            retry_seconds = None
            try:
                await dispatch(job)
                logger.info("job_completed", extra={"job_id": str(job.id), "kind": job.kind})
            except Exception as exc:
                error = type(exc).__name__
                if isinstance(exc, RetryAfter):
                    retry_seconds = (
                        exc.retry_after.total_seconds()
                        if isinstance(exc.retry_after, timedelta)
                        else exc.retry_after
                    )
                if isinstance(exc, ProviderRateLimited):
                    retry_seconds = exc.retry_seconds
                    if bot:
                        try:
                            await deliver(
                                bot,
                                engine,
                                settings.telegram_user_id,
                                f"quota:{datetime.now(UTC):%Y-%m-%d-%H}",
                                "Gemini временно отклонил запрос из-за лимита API. Сообщение сохранено, попробую позже. Команды /today и /status продолжают работать.",
                            )
                        except Exception:
                            pass
                logger.warning(
                    "job_failed",
                    extra={"job_id": str(job.id), "kind": job.kind, "error_type": error},
                )
                if isinstance(exc, (AuthenticationRequired, AccountError)) and bot:
                    with transaction(engine) as session:
                        enqueue_connection_notice(session, exc, datetime.now(UTC))
            finally:
                done.set()
                await lease_task
            with transaction(engine) as session:
                finish(
                    session,
                    job.id,
                    job.lease_token,
                    error_type=error,
                    retryable_delivery=error == "RetryAfter",
                )
                if retry_seconds is not None:
                    row = session.get(Job, job.id)
                    row.run_at = max(
                        row.run_at, datetime.now(UTC) + timedelta(seconds=retry_seconds)
                    )
                if error == "DeliveryUncertain":
                    row = session.get(Job, job.id)
                    row.status = "failed"
                    row.last_error = "DeliveryUncertain"

    async def scheduler():
        while not stop.is_set():
            now = datetime.now(UTC)
            # A lost singleton connection is fatal; supervisor restarts cleanly.
            singleton.execute(text("SELECT 1"))
            with transaction(engine) as session:
                from garmin_ai.conversation import prune_conversation

                prune_conversation(session, now)
                reconcile_failed_inbox(session)
                if (settings.token_dir / "garmin_tokens.json").exists():
                    schedule_sync(session, settings, now)
                if settings.backup_key.get_secret_value():
                    schedule_backup(session, now)
                enqueue(
                    session, "agent_proactive", {}, f"proactive:{int(now.timestamp()) // 1800}", now
                )
                enqueue(
                    session,
                    "agent_insights",
                    {
                        "sync_dependencies": [
                            str(identity)
                            for identity in session.scalars(
                                select(Job.id).where(
                                    Job.kind.in_(
                                        ["garmin_endpoint", "garmin_activities", "garmin_fit"]
                                    ),
                                    Job.status.in_(["pending", "running"]),
                                )
                            )
                        ]
                    },
                    f"insights:{int(now.timestamp()) // 21600}",
                    now,
                )
                upsert(
                    session,
                    AppState,
                    dict(key="runtime:heartbeat", value={"at": now.isoformat()}),
                    ["key"],
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass

    async def telegram_startup():
        while not stop.is_set():
            try:
                await bot.initialize()
                webhook = await bot.get_webhook_info()
                if webhook.url:
                    await serialize_webhook_delivery(bot, webhook, settings)
                bot_ready.set()
                if not webhook.url:
                    await poll(bot, engine, settings, stop, notifications_ready)
                else:
                    while not stop.is_set():
                        pending = await bot.get_webhook_info()
                        if pending.pending_update_count == 0:
                            notifications_ready.set()
                        else:
                            notifications_ready.clear()
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=1)
                        except TimeoutError:
                            pass
                return
            except Exception as exc:
                notifications_ready.clear()
                logger.warning("telegram_startup_failed", extra={"error_type": type(exc).__name__})
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass

    tasks = []
    try:
        if bot:
            tasks.append(asyncio.create_task(telegram_startup()))
            tasks.append(asyncio.create_task(worker(["telegram_ack"])))
            tasks.append(asyncio.create_task(worker(["telegram_control"])))
        tasks.extend(
            [
                asyncio.create_task(scheduler()),
                asyncio.create_task(worker(["garmin_endpoint", "garmin_activities", "garmin_fit"])),
                asyncio.create_task(
                    worker(
                        (
                            [
                                "telegram_update",
                                "telegram_failure",
                                "telegram_connection_notice",
                            ]
                            if bot
                            else []
                        )
                        + ["agent_proactive", "agent_insights"]
                    )
                ),
            ]
        )
        if settings.backup_key.get_secret_value():
            tasks.append(asyncio.create_task(worker(["backup"])))
        stopper = asyncio.create_task(stop.wait())
        completed, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            if task is not stopper:
                task.result()
    finally:
        stop.set()
        await drain_workers(tasks)
        try:
            if bot:
                await bot.shutdown()
        finally:
            if provider:
                provider.close()
            singleton.close()
            engine.dispose()


async def cached_transcription(engine, bot, provider, voice, update_id):
    key = f"telegram:transcript:{update_id}"
    with transaction(engine) as session:
        cached = session.get(AppState, key)
        if cached is not None:
            return cached.value["text"]
    transcript = await transcribe_voice(bot, provider, voice)
    with transaction(engine) as session:
        upsert(session, AppState, dict(key=key, value={"text": transcript}), ["key"])
    return transcript


async def serialize_webhook_delivery(bot, webhook, settings):
    secret = settings.telegram_webhook_secret.get_secret_value()
    if len(secret) < 16:
        raise ValueError("Configure GA_TELEGRAM_WEBHOOK_SECRET before using webhook delivery")
    await bot.set_webhook(
        url=webhook.url,
        max_connections=1,
        secret_token=secret,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
