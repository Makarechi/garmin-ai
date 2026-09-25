"""Single-host service supervisor with independent optional integration lanes."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import signal
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, cast, func, select, text, tuple_

from garmin_ai.accounts import AccountError
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.integrations import (
    IntegrationUnavailable,
    configured_instance,
    default_registry,
    integrations_explicit,
    onboarding_allows_instance,
)
from garmin_ai.jobs import (
    claim,
    enqueue,
    finish,
    renew,
    retire_garmin_jobs,
    schedule_backup,
)
from garmin_ai.llm import (
    ProviderConsentRequired,
    ProviderUnavailable,
)
from garmin_ai.models import AppState, Insight, Job, PendingQuestion, TelegramUpdate
from garmin_ai.normalize import upsert
from garmin_ai.operations import scheduled_backup
from garmin_ai.proactive import (
    can_notify,
    generate_insights,
    generate_questions,
    notification_decision,
    pending_insight_notices,
    reconcile_questions,
    reserve_insight_notice,
    select_question,
)

OPTIONAL_SYMBOLS = {
    "Bot": ("telegram", "Bot", "telegram"),
    "BadRequest": ("telegram.error", "BadRequest", "telegram"),
    "RetryAfter": ("telegram.error", "RetryAfter", "telegram"),
    "HTTPXRequest": ("telegram.request", "HTTPXRequest", "telegram"),
    "AuthenticationRequired": ("garmin_ai.garmin", "AuthenticationRequired", "garmin"),
    "GarminReader": ("garmin_ai.garmin", "GarminReader", "garmin"),
    "run_garmin_job": ("garmin_ai.sync", "run_garmin_job", "garmin"),
    "schedule_sync": ("garmin_ai.sync", "schedule_sync", "garmin"),
    "GeminiProvider": ("garmin_ai.llm", "GeminiProvider", "gemini"),
    "DeliveryUncertain": ("garmin_ai.telegram", "DeliveryUncertain", "telegram"),
    "DiaryDeferred": ("garmin_ai.telegram", "DiaryDeferred", "telegram"),
    "deliver": ("garmin_ai.telegram", "deliver", "telegram"),
    "owned_message": ("garmin_ai.telegram", "owned_message", "telegram"),
    "poll": ("garmin_ai.telegram", "poll", "telegram"),
    "process_message": ("garmin_ai.telegram", "process_message", "telegram"),
    "reconcile_failed_inbox": ("garmin_ai.telegram", "reconcile_failed_inbox", "telegram"),
}


def _optional_symbol(name):
    module, attribute, extra = OPTIONAL_SYMBOLS[name]
    try:
        value = getattr(importlib.import_module(module), attribute)
    except ImportError as exc:
        raise IntegrationUnavailable(
            f"{extra}:primary", f"install the '{extra}' extra to use this integration"
        ) from exc
    globals()[name] = value
    return value


def __getattr__(name):
    if name in OPTIONAL_SYMBOLS:
        return _optional_symbol(name)
    raise AttributeError(name)


def _bind_optional(*names):
    for name in names:
        current = globals()[name]
        default = _OPTIONAL_DEFAULTS[name]
        if current is not default:
            continue
        if name == "GarminReader" and current.__dict__.get("restore") is not _UNAVAILABLE_RESTORE:
            continue
        _optional_symbol(name)


class _UnavailableOptionalError(RuntimeError):
    pass


class DiaryDeferred(RuntimeError):
    """Retryable diary deferral available without an optional channel SDK."""


_LOCAL_CAPTION_COMMANDS = {
    "/newtracker",
    "/preview",
    "/confirm_tracker",
    "/remove_field",
    "/privacy",
    "/cancel",
    "/history",
    "/undo",
    "/today",
    "/status",
    "/goals",
    "/conversation",
    "/forget_conversation",
    "/pause",
    "/resume",
    "/help",
    "/start",
    "/debug",
}


def _local_caption_command(caption: str | None) -> bool:
    return bool(caption and caption.strip().split(maxsplit=1)[0] in _LOCAL_CAPTION_COMMANDS)


class _UnavailableReader:
    def __init__(self, *_args, **_kwargs):
        self.on_success = None

    @classmethod
    def restore(cls, _path):
        raise _UnavailableOptionalError("Garmin integration is unavailable")


async def deliver(*args, **kwargs):
    return await _optional_symbol("deliver")(*args, **kwargs)


async def poll(*args, **kwargs):
    return await _optional_symbol("poll")(*args, **kwargs)


def process_message(*args, **kwargs):
    return _optional_symbol("process_message")(*args, **kwargs)


def owned_message(*args, **kwargs):
    return _optional_symbol("owned_message")(*args, **kwargs)


def reconcile_failed_inbox(*args, **kwargs):
    return _optional_symbol("reconcile_failed_inbox")(*args, **kwargs)


def run_garmin_job(*args, **kwargs):
    return _optional_symbol("run_garmin_job")(*args, **kwargs)


def schedule_sync(*args, **kwargs):
    return _optional_symbol("schedule_sync")(*args, **kwargs)


Bot: Any = None
HTTPXRequest: Any = None
GeminiProvider: Any = None
BadRequest = _UnavailableOptionalError
RetryAfter = _UnavailableOptionalError
AuthenticationRequired = _UnavailableOptionalError
DeliveryUncertain = _UnavailableOptionalError
GarminReader = _UnavailableReader

_UNAVAILABLE_RESTORE = _UnavailableReader.__dict__["restore"]
_OPTIONAL_DEFAULTS = {name: globals()[name] for name in OPTIONAL_SYMBOLS}


@contextmanager
def initiative_delivery_fence(engine):
    """Serialize policy changes with a bounded send without a DB transaction."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if not connection.scalar(text("SELECT pg_try_advisory_lock_shared(72104621)")):
            raise DiaryDeferred("Initiative policy is changing")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock_shared(72104621)"))


async def deliver_current_insight(bot, engine, settings, insight_id, *, channel_instance=None):
    from garmin_ai.replay import replay_pending_condition

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as reservation:
        if not reservation.scalar(text("SELECT pg_try_advisory_lock(72104619)")):
            raise DiaryDeferred("Insight delivery awaits normalization")
        try:
            with transaction(engine) as session:
                if session.scalar(select(replay_pending_condition())):
                    raise DiaryDeferred("Insight delivery awaits complete archive replay")
                insight = session.get(Insight, insight_id)
                if insight is None or insight.status != "accepted":
                    return
                from garmin_ai.scenario_packs import insight_enabled

                if not insight_enabled(session, insight):
                    metric = insight.dedup_key.split(":")[1]
                    reserved_notice = session.get(AppState, f"insight:last:{metric}")
                    if reserved_notice is not None and reserved_notice.value.get(
                        "reservation"
                    ) == str(insight.id):
                        session.delete(reserved_notice)
                    return
                if not reserve_insight_notice(session, settings, datetime.now(UTC), insight):
                    return
                statement = insight.statement
                metric = insight.dedup_key.split(":")[1]
            status = "delivered"
            with initiative_delivery_fence(engine):
                with transaction(engine) as session:
                    if not can_notify(session, settings, datetime.now(UTC), include_budget=False):
                        return
                try:
                    await asyncio.wait_for(
                        deliver(
                            bot,
                            engine,
                            settings.telegram_user_id,
                            f"insight:{insight_id}",
                            statement,
                            **({"channel_instance": channel_instance} if channel_instance else {}),
                        ),
                        timeout=60,
                    )
                except (DeliveryUncertain, TimeoutError):
                    status = "uncertain"
                with transaction(engine) as session:
                    insight = session.get(Insight, insight_id)
                    if insight is not None and insight.status == "accepted":
                        insight.status = status
                    upsert(
                        session,
                        AppState,
                        dict(
                            key=f"insight:last:{metric}",
                            value={"at": datetime.now(UTC).isoformat()},
                        ),
                        ["key"],
                    )
        finally:
            reservation.execute(text("SELECT pg_advisory_unlock(72104619)"))


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


def claim_ready_job(
    engine,
    kinds,
    backups_enabled,
    has_bot,
    provider_settings=None,
    source_instance_id=None,
    notification_gate=None,
):
    """Keep queue queries off the event loop used for Telegram networking."""
    with transaction(engine) as session:
        if source_instance_id is not None:
            from garmin_ai.onboarding import source_instance_selected

            if not source_instance_selected(session, source_instance_id):
                retire_garmin_jobs(session, datetime.now(UTC))
                kinds = [kind for kind in kinds if not kind.startswith("garmin_")]
        if (
            has_bot
            and session.scalar(
                select(TelegramUpdate.id).where(TelegramUpdate.status == "pending").limit(1)
            )
            is not None
        ):
            kinds = [kind for kind in kinds if kind not in {"agent_proactive", "agent_insights"}]
        # The event-loop snapshot can turn stale while this queue query runs
        # in a thread (for example, when webhook backlog reappears).
        if notification_gate is not None and not notification_gate.is_set():
            kinds = [
                kind
                for kind in kinds
                if kind not in {"agent_proactive", "agent_insights", "telegram_debug_notice"}
            ]
        return (
            claim(
                session,
                kinds=kinds,
                backups_enabled=backups_enabled,
                provider_settings=provider_settings,
            )
            if kinds
            else None
        )


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


async def deliver_connection_notice(bot, engine, user_id, payload, *, channel_instance=None):
    category = payload["category"]
    message = (
        "Синхронизация Garmin остановлена: владелец аккаунта не подтверждён или не совпадает с владельцем базы. История и дневник доступны. Проверьте исходный аккаунт; для другого владельца нужен отдельный экземпляр. Для старой базы без привязки используйте локальный enroll-account --confirm-existing-owner."
        if category == "account-binding"
        else "Garmin требует повторного входа. История и дневник доступны. Остановите процесс garmin-ai worker (Ctrl+C в его терминале или через диспетчер служб), выполните uv run garmin-ai login и запустите worker тем же способом. Если используете Compose с сервисом worker: docker compose stop worker → uv run garmin-ai login → docker compose start worker."
    )
    await deliver(
        bot,
        engine,
        user_id,
        payload["key"],
        message,
        **({"channel_instance": channel_instance} if channel_instance else {}),
    )


async def run(settings: Settings | None = None):
    from garmin_ai.storage_files import exclusive_files

    settings = settings or Settings()
    with exclusive_files(settings):
        await _run(settings)


async def _run(settings):
    setup_logging()
    logger = logging.getLogger("garmin_ai")
    engine = make_engine(settings)
    from garmin_ai.accounts import apply_instance_settings, effective_owner_settings

    singleton = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    if not singleton.scalar(text("SELECT pg_try_advisory_lock(72104620)")):
        singleton.close()
        engine.dispose()
        raise RuntimeError("Another Garmin AI runtime is already running")
    onboarding_preferences = None
    onboarding_model_categories = None
    try:
        with transaction(engine) as session:
            apply_instance_settings(session, settings)
            settings = effective_owner_settings(session, settings)
            from garmin_ai.canonical_events import backfill_canonical_events
            from garmin_ai.definitions import ensure_system_definitions
            from garmin_ai.metric_definitions import ensure_system_metric_definitions
            from garmin_ai.scenario_packs import ensure_scenario_packs

            ensure_system_definitions(session, backfill=True)
            ensure_system_metric_definitions(session, backfill=True)
            backfill_canonical_events(session)
            ensure_scenario_packs(session)
            saved_onboarding = session.get(AppState, "preferences:onboarding")
            if saved_onboarding is not None:
                onboarding_preferences = saved_onboarding.value
            from garmin_ai.onboarding import selected_model_categories

            onboarding_model_categories = selected_model_categories(session)
    except BaseException:
        singleton.close()
        engine.dispose()
        raise
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    archive = LocalArchive(settings.data_dir / "raw")
    reader = None
    registry = default_registry()
    model_instance = configured_instance(settings, "model", "gemini")
    provider = None
    model_enabled = onboarding_model_categories is None or bool(onboarding_model_categories)
    if model_enabled and (model_instance is not None or not integrations_explicit(settings)):
        _bind_optional("GeminiProvider")
        try:
            provider = (
                GeminiProvider(settings, instance_id=model_instance.id)
                if model_instance is not None and model_instance.id != "model:gemini:primary"
                else GeminiProvider(settings)
            )
            from garmin_ai.provider_gate import ProviderGate

            provider.request_gate = ProviderGate(engine, settings)
        except (IntegrationUnavailable, ProviderUnavailable) as exc:
            logger.info(
                "model_integration_unavailable",
                extra={"provider": "gemini", "error_type": type(exc).__name__},
            )
    telegram_enabled = bool(
        settings.telegram_bot_token.get_secret_value() and settings.telegram_user_id
    )
    telegram_instance = configured_instance(settings, "channel", "telegram")
    from garmin_ai.channels import ChannelInstanceRef
    from garmin_ai.integrations import channel_instance_id

    telegram_channel_instance = ChannelInstanceRef(
        channel="telegram",
        instance_id=channel_instance_id(telegram_instance),
    )
    if integrations_explicit(settings):
        telegram_enabled = telegram_enabled and telegram_instance is not None
    if telegram_instance is not None:
        telegram_enabled = telegram_enabled and onboarding_allows_instance(
            telegram_instance, onboarding_preferences
        )
    if telegram_enabled:
        from garmin_ai.onboarding import channel_instance_selected

        with transaction(engine) as session:
            telegram_enabled = channel_instance_selected(session, telegram_channel_instance)
    if telegram_enabled:
        try:
            if telegram_instance is not None:
                status = registry.status(telegram_instance, settings)
                if not status.available:
                    raise IntegrationUnavailable(
                        telegram_instance.id, status.reason or "channel integration unavailable"
                    )
            _bind_optional(
                "Bot",
                "BadRequest",
                "RetryAfter",
                "HTTPXRequest",
                "DeliveryUncertain",
                "DiaryDeferred",
                "deliver",
                "owned_message",
                "poll",
                "process_message",
                "reconcile_failed_inbox",
            )
        except IntegrationUnavailable as exc:
            telegram_enabled = False
            logger.warning(
                "channel_integration_unavailable",
                extra={"provider": "telegram", "error_type": type(exc).__name__},
            )
    from garmin_ai.integrations import module_available

    garmin_instance = configured_instance(settings, "source", "garmin")
    garmin_selected = not integrations_explicit(settings) or garmin_instance is not None
    if garmin_instance is not None:
        garmin_selected = garmin_selected and onboarding_allows_instance(
            garmin_instance, onboarding_preferences
        )
    garmin_enabled = module_available("garminconnect") and garmin_selected
    if garmin_enabled:
        try:
            if garmin_instance is not None:
                status = registry.status(garmin_instance, settings)
                if not status.available:
                    raise IntegrationUnavailable(
                        garmin_instance.id, status.reason or "source integration unavailable"
                    )
            _bind_optional(
                "AuthenticationRequired", "GarminReader", "run_garmin_job", "schedule_sync"
            )
        except IntegrationUnavailable as exc:
            garmin_enabled = False
            logger.warning(
                "source_integration_unavailable",
                extra={"provider": "garmin", "error_type": type(exc).__name__},
            )
    if not garmin_enabled:
        with transaction(engine) as session:
            retire_garmin_jobs(session, datetime.now(UTC))
    polling_request = HTTPXRequest(connection_pool_size=1) if telegram_enabled else None
    bot = (
        Bot(settings.telegram_bot_token.get_secret_value(), get_updates_request=polling_request)
        if telegram_enabled
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

    async def deliver_neutral_initiatives(limit=3):
        from garmin_ai.channels import DeliveryAttempt, DeliveryState
        from garmin_ai.dialogue import recover_expired_outbox_leases
        from garmin_ai.initiative_rules import claim_due_initiative, finish_initiative_attempt
        from garmin_ai.share_policy import channel_consent_delivery_fence

        for _ in range(limit):
            now = datetime.now(UTC)
            # Claiming recovers expired leases under the replay lock. Complete
            # that transaction before taking the consent delivery fence.
            with transaction(engine) as session:
                recover_expired_outbox_leases(session, now)
            try:
                with initiative_delivery_fence(engine), channel_consent_delivery_fence(engine):
                    with transaction(engine) as session:
                        lease = claim_due_initiative(
                            session,
                            now,
                            recover=False,
                            supported_destinations=(
                                frozenset(
                                    {
                                        f"{telegram_channel_instance.channel}:"
                                        f"{telegram_channel_instance.instance_id}"
                                    }
                                )
                                if bot is not None
                                else frozenset()
                            ),
                        )
                    if lease is None:
                        return
                    target = lease.intent.channel_instance
                    if (
                        target.channel == "telegram"
                        and target == telegram_channel_instance
                        and bot is not None
                    ):
                        from garmin_ai.telegram_adapter import TelegramChannel

                        adapter = TelegramChannel(
                            bot,
                            settings.telegram_user_id,
                            channel_instance=telegram_channel_instance,
                        )
                        try:
                            attempt = await asyncio.wait_for(
                                adapter.deliver(lease.intent, now=now), timeout=60
                            )
                        except Exception as exc:
                            attempt = DeliveryAttempt(
                                intent_id=lease.intent.intent_id,
                                state=DeliveryState.UNCERTAIN,
                                reason=f"channel adapter raised {type(exc).__name__}",
                            )
                    else:
                        attempt = DeliveryAttempt(
                            intent_id=lease.intent.intent_id,
                            state=DeliveryState.QUEUED,
                            reason="configured channel adapter is not running",
                            retry_after=now + timedelta(minutes=15),
                        )
                    with transaction(engine) as session:
                        finish_initiative_attempt(session, lease, attempt, datetime.now(UTC))
            except DiaryDeferred:
                return

    async def dispatch(job):
        if job.kind.startswith("garmin_"):
            await run_blocking(garmin_job, job.kind, job.payload)
        elif job.kind == "raw_replay":
            from garmin_ai.replay import run_replay

            await run_blocking(run_replay, engine, archive, settings, job.payload)
        elif job.kind == "storage_check":
            from garmin_ai.storage_alerts import check_storage

            await run_blocking(check_storage, engine, settings)
        elif job.kind == "telegram_storage_notice":
            from garmin_ai.storage_alerts import deliver_storage_notice

            await deliver_storage_notice(bot, engine, settings, job.payload)
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
            await deliver_connection_notice(
                bot,
                engine,
                settings.telegram_user_id,
                job.payload,
                channel_instance=telegram_channel_instance,
            )
        elif job.kind == "telegram_debug_notice":
            from garmin_ai.debug import can_deliver, notice_text

            with transaction(engine) as session:
                send_notice = can_deliver(session, job.payload)
            if send_notice:
                await deliver(
                    bot,
                    engine,
                    settings.telegram_user_id,
                    f"debug-notice:{job.id}",
                    notice_text(job.payload),
                    channel_instance=telegram_channel_instance,
                )
        elif job.kind == "telegram_failure":
            if bot is None:
                raise RuntimeError("Telegram is not configured")
            await deliver(
                bot,
                engine,
                settings.telegram_user_id,
                f"failure:{job.payload['update_id']}",
                "Не удалось обработать сообщение после повторных попыток. Пришлите его заново или воспользуйтесь кнопками и /help.",
                channel_instance=telegram_channel_instance,
            )
        elif job.kind == "telegram_provider_notice":
            from garmin_ai.provider_gate import QUOTA_NOTICE

            await deliver(
                bot,
                engine,
                settings.telegram_user_id,
                job.payload["outbox_key"],
                QUOTA_NOTICE,
                channel_instance=telegram_channel_instance,
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
            raw_channel = update.get("_channel_instance")
            ingress_channel = (
                ChannelInstanceRef.model_validate(raw_channel)
                if raw_channel is not None
                else ChannelInstanceRef(channel="telegram", instance_id="primary")
            )
            if ingress_channel != telegram_channel_instance:
                with transaction(engine) as session:
                    from garmin_ai.telegram_adapter import set_update_status

                    set_update_status(session, job.payload["update_id"], "invalid")
                return
            message = owned_message(update, settings.telegram_user_id)
            if message is None:
                raise ValueError("Unauthorized Telegram update")
            message_provider = provider
            transcript = None
            if message.get("voice") and not has_reply:
                voice = message["voice"]
                destination = (
                    f"{telegram_channel_instance.channel}:{telegram_channel_instance.instance_id}"
                )
                caption_answer = _local_caption_command(message.get("caption"))
                if (message.get("caption") or "").strip() and not caption_answer:
                    caption_answer = _guided_caption_answers_form(engine, message, destination)
                if (message.get("caption") or "").strip() and not caption_answer:
                    caption_answer = _caption_answers_setup_or_close(engine, message, destination)
                if (
                    caption_answer
                    or provider is None
                    or _caption_selects_tracker(engine, message, destination, settings.locale)
                ):
                    transcript = ""
                else:
                    try:
                        transcript = await cached_transcription(
                            engine,
                            bot,
                            provider,
                            voice,
                            job.payload["update_id"],
                            destination_instance_id=destination,
                            reply_to_message_id=message.get("reply_to_message", {}).get(
                                "message_id"
                            ),
                            caption=message.get("caption"),
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
                            channel_instance=telegram_channel_instance,
                        )
                        with transaction(engine) as session:
                            from garmin_ai.telegram_adapter import set_update_status

                            set_update_status(session, job.payload["update_id"], "invalid")
                        return
            response = await run_blocking(
                process_message,
                engine,
                message_provider,
                settings,
                job.payload["update_id"],
                transcript,
            )
            if response is None:
                return
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
                channel_instance=telegram_channel_instance,
            )
        elif job.kind == "agent_proactive":
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as reservation:
                if not reservation.scalar(text("SELECT pg_try_advisory_lock(72104619)")):
                    raise DiaryDeferred("Diary update in progress")
                try:
                    with transaction(engine) as session:
                        session.info["channel_destination_instance_id"] = (
                            f"{telegram_channel_instance.channel}:"
                            f"{telegram_channel_instance.instance_id}"
                        )
                        from garmin_ai.accounts import effective_owner_settings

                        owner_settings = effective_owner_settings(session, settings)
                        now = datetime.now(UTC)
                        reconcile_questions(session)
                        from garmin_ai.replay import replay_pending_condition

                        replay_pending = bool(session.scalar(select(replay_pending_condition())))
                        allow_context = (
                            not job.payload.get("context_sync_failures") and not replay_pending
                        )
                        generate_questions(
                            session, owner_settings, now, allow_context=allow_context
                        )
                        question = (
                            select_question(
                                session, owner_settings, now, allow_context=allow_context
                            )
                            if notifications_ready.is_set() and provider
                            else None
                        )
                    if question:
                        with initiative_delivery_fence(engine):
                            try:
                                with transaction(engine) as session:
                                    current = session.get(
                                        PendingQuestion, question.id, populate_existing=True
                                    )
                                    policy = notification_decision(
                                        session,
                                        owner_settings,
                                        datetime.now(UTC),
                                        include_budget=False,
                                    )
                                    if (
                                        current is None
                                        or current.status != "sending"
                                        or policy.action != "allow"
                                    ):
                                        if current is not None and current.status == "sending":
                                            current.status = (
                                                "cancelled"
                                                if policy.action == "cancel"
                                                else "pending"
                                            )
                                            if policy.reason == "owner_paused":
                                                current.evidence = {
                                                    **current.evidence,
                                                    "cancel_reason": "owner_pause",
                                                }
                                            current.sent_at = None
                                        return
                                await asyncio.wait_for(
                                    deliver(
                                        bot,
                                        engine,
                                        settings.telegram_user_id,
                                        f"question:{question.id}",
                                        question.text,
                                        channel_instance=telegram_channel_instance,
                                    ),
                                    timeout=60,
                                )
                                with transaction(engine) as session:
                                    session.get(PendingQuestion, question.id).status = "sent"
                            except (DeliveryUncertain, TimeoutError):
                                with transaction(engine) as session:
                                    session.get(PendingQuestion, question.id).status = "uncertain"
                                raise
                            except DiaryDeferred:
                                with transaction(engine) as session:
                                    from garmin_ai.proactive import release_unsent_question

                                    release_unsent_question(session, question.id)
                                raise
                finally:
                    reservation.execute(text("SELECT pg_advisory_unlock(72104619)"))
            await deliver_neutral_initiatives()
            if (
                not allow_context
                and not job.payload.get("garmin_paused")
                and not replay_pending
                and datetime.now(UTC) < datetime.fromisoformat(job.payload["context_expires_at"])
            ):
                raise DiaryDeferred("Context generation awaits recovered synchronization")
        elif job.kind == "agent_insights":
            from garmin_ai.replay import replay_pending_condition

            with transaction(engine) as session:
                from garmin_ai.accounts import effective_owner_settings
                from garmin_ai.integration import paused

                if job.payload.get("garmin_paused") or paused(session, datetime.now(UTC)):
                    # Consume this scheduled cycle without a claim from stale
                    # Garmin evidence; a later cycle resumes after recovery.
                    return
                session.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
                if session.scalar(select(replay_pending_condition())):
                    raise DiaryDeferred("Insights await complete archive replay")
                owner_settings = effective_owner_settings(session, settings)
                generate_insights(session, datetime.now(UTC), owner_settings.timezone)
                accepted = pending_insight_notices(session, datetime.now(UTC))
            with transaction(engine) as session:
                session.info["channel_destination_instance_id"] = (
                    f"{telegram_channel_instance.channel}:{telegram_channel_instance.instance_id}"
                )
                allowed = can_notify(session, settings, datetime.now(UTC), include_budget=False)
            if notifications_ready.is_set() and allowed:
                for insight in accepted:
                    await deliver_current_insight(
                        bot,
                        engine,
                        settings,
                        insight.id,
                        channel_instance=telegram_channel_instance,
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
                    or kind not in {"agent_proactive", "agent_insights", "telegram_debug_notice"}
                    or notifications_ready.is_set()
                )
            ]
            job = await run_blocking(
                claim_ready_job,
                engine,
                available,
                bool(settings.backup_key.get_secret_value()),
                bool(bot),
                settings,
                (garmin_instance.id if garmin_instance is not None else "source:garmin:primary")
                if any(kind.startswith("garmin_") for kind in available)
                else None,
                notifications_ready if bot else None,
            )
            if job is None:
                await asyncio.sleep(1)
                continue
            done = asyncio.Event()
            lease_task = asyncio.create_task(maintain_lease(job.id, job.lease_token, done))
            error = None
            retry_seconds = None
            provider_failure = False
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
                if isinstance(exc, ProviderUnavailable):
                    provider_failure = True
                    retry_seconds = exc.retry_seconds
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
                    retry_at=datetime.now(UTC) + timedelta(seconds=retry_seconds)
                    if provider_failure and retry_seconds is not None
                    else None,
                )
                if retry_seconds is not None and not provider_failure:
                    row = session.get(Job, job.id)
                    row.run_at = max(
                        row.run_at, datetime.now(UTC) + timedelta(seconds=retry_seconds)
                    )
                if error == "DeliveryUncertain":
                    row = session.get(Job, job.id)
                    row.status = "failed"
                    row.last_error = "DeliveryUncertain"
                if error and bot:
                    from garmin_ai.debug import queue_error_notice

                    queue_error_notice(session, job.kind, error)

    async def scheduler():
        while not stop.is_set():
            now = datetime.now(UTC)
            # A lost singleton connection is fatal; supervisor restarts cleanly.
            singleton.execute(text("SELECT 1"))
            with transaction(engine) as session:
                from garmin_ai.conversation import prune_conversation
                from garmin_ai.dialogue import prune_neutral_analysis

                prune_conversation(session, now)
                prune_neutral_analysis(session, now)
                if telegram_enabled:
                    reconcile_failed_inbox(session)
                from garmin_ai.replay import schedule_replay

                # A large offline projection can hold the normalization lock.
                # Skip this scheduling tick instead of blocking lease renewals
                # on the async event loop behind that database transaction.
                if session.scalar(text("SELECT pg_try_advisory_xact_lock(72104619)")):
                    schedule_replay(session, now)
                    from garmin_ai.onboarding import source_instance_selected

                    source_id = (
                        garmin_instance.id
                        if garmin_instance is not None
                        else "source:garmin:primary"
                    )
                    source_selected = source_instance_selected(session, source_id)
                    if not source_selected:
                        retire_garmin_jobs(session, now)
                    if (
                        garmin_enabled
                        and source_selected
                        and (settings.token_dir / "garmin_tokens.json").exists()
                    ):
                        schedule_sync(session, settings, now)
                if settings.backup_key.get_secret_value():
                    schedule_backup(session, now)
                    from garmin_ai.storage_alerts import schedule_storage_check

                    schedule_storage_check(session, settings, now)
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
                    await poll(
                        bot,
                        engine,
                        settings,
                        stop,
                        notifications_ready,
                        polling_request=polling_request,
                        channel_instance=telegram_channel_instance,
                    )
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
            tasks.append(
                asyncio.create_task(
                    worker(["telegram_ack", "telegram_provider_notice", "telegram_storage_notice"])
                )
            )
            tasks.append(asyncio.create_task(worker(["telegram_control", "telegram_debug_notice"])))
        tasks.extend(
            [
                asyncio.create_task(scheduler()),
                asyncio.create_task(worker(["raw_replay"])),
                asyncio.create_task(
                    worker(
                        ["garmin_endpoint", "garmin_activities", "garmin_fit"]
                        if garmin_enabled
                        else []
                    )
                ),
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
            tasks.append(asyncio.create_task(worker(["storage_check"])))
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


def _message_sent_at(message, fallback):
    sent = message.get("date")
    if isinstance(sent, (int, float)):
        return datetime.fromtimestamp(sent, UTC)
    if isinstance(sent, str):
        sent_at = datetime.fromisoformat(sent)
        return sent_at if sent_at.tzinfo is not None else sent_at.replace(tzinfo=UTC)
    return fallback


def _guided_caption_answers_form(engine, message, destination_instance_id):
    if not (message.get("caption") or "").strip():
        return False
    from garmin_ai.agent import pending_clarification
    from garmin_ai.conversation import is_analytic_reply

    with transaction(engine) as session:
        session.info["channel_destination_instance_id"] = destination_instance_id
        sent_at = _message_sent_at(message, datetime.now(UTC))
        pending = pending_clarification(session, datetime.now(UTC))
        if pending is None:
            pending = pending_clarification(session, sent_at)
        return bool(
            pending
            and (pending.value.get("chat_form") or pending.value.get("button") == "tracker_select")
            and pending.value.get("channel_instance_id", "telegram:primary")
            == destination_instance_id
            and not is_analytic_reply(
                session, message.get("reply_to_message", {}).get("message_id")
            )
        )


def _caption_answers_setup_or_close(engine, message, destination_instance_id):
    """Use the voice message's sent time before expiring a local setup draft."""
    from garmin_ai.agent import pending_clarification
    from garmin_ai.conversation import is_analytic_reply
    from garmin_ai.tracker_chat_setup import active_setup_row

    with transaction(engine) as session:
        session.info["channel_destination_instance_id"] = destination_instance_id
        sent_at = _message_sent_at(message, datetime.now(UTC))
        if is_analytic_reply(session, message.get("reply_to_message", {}).get("message_id")):
            return False
        pending = pending_clarification(session, datetime.now(UTC))
        if pending is None:
            pending = pending_clarification(session, sent_at)
        if (
            pending
            and pending.value.get("channel_instance_id", "telegram:primary")
            == destination_instance_id
            and (pending.value.get("chat_form") or pending.value.get("chat_close"))
        ):
            return True
        return active_setup_row(session, at=sent_at) is not None


def _caption_selects_tracker(engine, message, destination_instance_id, locale):
    caption = (message.get("caption") or "").strip()
    if not caption:
        return False
    from garmin_ai.conversation import is_analytic_reply
    from garmin_ai.tracker_chat_selection import select_tracker_actions

    with transaction(engine) as session:
        session.info["channel_destination_instance_id"] = destination_instance_id
        if is_analytic_reply(session, message.get("reply_to_message", {}).get("message_id")):
            return False
        return bool(
            select_tracker_actions(
                session,
                caption,
                locale=locale,
                destination=destination_instance_id,
            )
        )


async def cached_transcription(
    engine,
    bot,
    provider,
    voice,
    update_id,
    *,
    destination_instance_id="telegram:primary",
    reply_to_message_id=None,
    caption=None,
):
    from garmin_ai.diary_forms import obvious_urgent_symptoms
    from garmin_ai.share_policy import model_consent_delivery_fence

    if caption and obvious_urgent_symptoms(caption):
        raise ProviderConsentRequired("Emergency caption stays local")
    with model_consent_delivery_fence(engine):
        return await _cached_transcription_fenced(
            engine,
            bot,
            provider,
            voice,
            update_id,
            destination_instance_id=destination_instance_id,
            reply_to_message_id=reply_to_message_id,
            caption=caption,
        )


async def _cached_transcription_fenced(
    engine,
    bot,
    provider,
    voice,
    update_id,
    *,
    destination_instance_id="telegram:primary",
    reply_to_message_id=None,
    caption=None,
):
    key = f"telegram:transcript:{update_id}"
    with transaction(engine) as session:
        from garmin_ai.agent import pending_clarification
        from garmin_ai.conversation import is_analytic_reply
        from garmin_ai.jobs import telegram_order
        from garmin_ai.models import EventDefinitionVersion, TelegramUpdate
        from garmin_ai.provider_gate import require_onboarding_categories
        from garmin_ai.share_policy import version_sharing_allowed
        from garmin_ai.tracker_chat_setup import active_setup_row

        session.info["channel_destination_instance_id"] = destination_instance_id
        require_onboarding_categories(session, {"audio"})
        stored_update = session.get(TelegramUpdate, update_id)
        sent_at = None
        if stored_update is not None:
            raw_sent = stored_update.payload.get("message", {}).get("date")
            sent_at = (
                datetime.fromtimestamp(raw_sent, UTC)
                if isinstance(raw_sent, (int, float))
                else datetime.fromisoformat(raw_sent)
                if isinstance(raw_sent, str)
                else stored_update.received_at
            )
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=UTC)
            earlier = session.scalar(
                select(Job.id)
                .join(
                    TelegramUpdate,
                    TelegramUpdate.id == cast(Job.payload["update_id"].astext, BigInteger),
                )
                .where(
                    TelegramUpdate.status == "pending",
                    Job.kind.in_(["telegram_update", "telegram_control"]),
                    Job.status.in_(["pending", "running"]),
                    func.coalesce(Job.payload["channel_instance_id"].astext, "telegram:primary")
                    == destination_instance_id,
                    telegram_order()
                    < tuple_(
                        stored_update.payload.get("_ordering_epoch", 0),
                        stored_update.payload["update_id"],
                    ),
                )
                .limit(1)
            )
            if earlier is not None:
                raise DiaryDeferred("Earlier Telegram mutation must finish before transcription")
        pending = pending_clarification(session, datetime.now(UTC))
        if sent_at is not None:
            session.info["conversation_now"] = sent_at
            pending = pending or pending_clarification(session, sent_at)
        setup = active_setup_row(session, at=sent_at)
        if (caption or "").lstrip().startswith("/"):
            raise ProviderConsentRequired("Captioned local command audio stays local")
        if (
            pending is not None
            and pending.value.get("button") == "tracker_select"
            and not is_analytic_reply(session, reply_to_message_id)
        ):
            raise ProviderConsentRequired("Tracker selection audio stays local")
        if (
            setup is not None
            and (
                setup.value.get("privacy") == "sensitive"
                or (caption or "").strip().casefold().startswith("/privacy ")
            )
            and not is_analytic_reply(session, reply_to_message_id)
        ):
            raise ProviderConsentRequired("Sensitive tracker setup audio stays local")
        if (
            pending is not None
            and pending.value.get("definition_version_id")
            and pending.value.get("channel_instance_id", "telegram:primary")
            == destination_instance_id
            and not is_analytic_reply(session, reply_to_message_id)
        ):
            version_id = UUID(pending.value["definition_version_id"])
            version = session.get(EventDefinitionVersion, version_id)
            categories = {"schema", "facts"}
            if version is not None and version.privacy == "sensitive":
                categories.add("original_text")
            if not version_sharing_allowed(
                session,
                version_id,
                destination_kind="model",
                destination_instance_id=getattr(provider, "instance_id", "model:gemini:primary"),
                categories=categories,
            ):
                raise ProviderConsentRequired("Tracker audio sharing is not allowed")
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
        allowed_updates=["message", "edited_message", "callback_query"],
        drop_pending_updates=False,
    )


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
