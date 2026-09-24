"""Private Telegram inbox, durable replies, and a shared diary/analysis agent."""

import asyncio
import hashlib
import logging
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, cast, func, or_, select, tuple_
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, RetryAfter

from garmin_ai.agent import (
    AnalysisBudget,
    answer_question,
    apply_command,
    interpret,
    pending_clarification,
)
from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.db import transaction, writer_guard
from garmin_ai.diary_labels import diary_label
from garmin_ai.events import Conflict, EventInput, create_event, serialize, undo_last, update_event
from garmin_ai.jobs import enqueue, telegram_order
from garmin_ai.llm import ProviderConsentRequired
from garmin_ai.models import (
    AppState,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    HealthDay,
    Job,
    TelegramUpdate,
)
from garmin_ai.normalize import upsert
from garmin_ai.pending_state import pending_key
from garmin_ai.queries import data_freshness
from garmin_ai.telegram_adapter import (
    TELEGRAM_INSTANCE,
    authenticated_message,
    record_neutral_ingress,
    set_update_status,
)
from garmin_ai.telegram_format import message_parts

__all__ = ["diary_label"]

KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("☕ Кофе", callback_data="coffee"),
            InlineKeyboardButton("🤕 Мигрень", callback_data="migraine"),
        ],
        [
            InlineKeyboardButton("✅ Закончилась", callback_data="end"),
            InlineKeyboardButton("💊 Лекарство", callback_data="medication"),
        ],
        [
            InlineKeyboardButton("🍺 Алкоголь", callback_data="alcohol"),
            InlineKeyboardButton("📝 Заметка", callback_data="note"),
        ],
    ]
)


def scenario_keyboard(session):
    """Render enabled built-ins and active generated tracker actions."""
    from garmin_ai.accounts import owner
    from garmin_ai.scenario_packs import pack_enabled
    from garmin_ai.share_policy import version_sharing_allowed
    from garmin_ai.tracker_forms import available_actions

    def enabled(key):
        return pack_enabled(session, key) and pack_enabled(session, key, "visibility")

    rows = []
    first = []
    if enabled("caffeine"):
        first.append(InlineKeyboardButton("☕ Кофе", callback_data="coffee"))
    if enabled("migraine"):
        first.append(InlineKeyboardButton("🤕 Мигрень", callback_data="migraine"))
    if first:
        rows.append(first)
    if enabled("migraine"):
        rows.append(
            [
                InlineKeyboardButton("✅ Закончилась", callback_data="end"),
                InlineKeyboardButton("💊 Лекарство", callback_data="medication"),
            ]
        )
    if enabled("general_diary"):
        rows.append(
            [
                InlineKeyboardButton("🍺 Алкоголь", callback_data="alcohol"),
                InlineKeyboardButton("📝 Заметка", callback_data="note"),
            ]
        )
    generated = [
        InlineKeyboardButton(action.label, callback_data=action.id)
        for action in available_actions(
            session,
            locale=session.info.get("locale") or owner(session).locale,
        )
        if session.info.get("channel_destination_instance_id")
        and version_sharing_allowed(
            session,
            action.definition_version_id,
            destination_kind="channel",
            destination_instance_id=session.info["channel_destination_instance_id"],
            categories={"schema"},
        )
    ]
    rows.extend(generated[index : index + 2] for index in range(0, len(generated), 2))
    return InlineKeyboardMarkup(rows)


def callback_pack(callback):
    if callback in {"coffee", "coffee:unspecified"} or (callback and callback.startswith("c:")):
        return "caffeine"
    if callback in {"migraine", "end", "medication"}:
        return "migraine"
    if callback in {"alcohol", "note"}:
        return "general_diary"
    return None


def owned_message(update: dict, owner_id: int):
    return authenticated_message(update, owner_id)


def _ingress_state_key(name: str, channel_instance: ChannelInstanceRef) -> str:
    return (
        name
        if channel_instance == TELEGRAM_INSTANCE
        else f"{name}:{channel_instance.channel}:{channel_instance.instance_id}"
    )


def _storage_update_id(session, provider_update_id: int, channel_instance: ChannelInstanceRef):
    existing = session.get(TelegramUpdate, provider_update_id)
    if existing is None or existing.payload.get(
        "_channel_instance", TELEGRAM_INSTANCE.model_dump()
    ) == (channel_instance.model_dump()):
        return provider_update_id
    namespace = f"{channel_instance.channel}:{channel_instance.instance_id}:{provider_update_id}"
    digest = hashlib.sha256(namespace.encode()).digest()
    storage_number = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
    storage_id = -(storage_number or 1)
    collision = session.get(TelegramUpdate, storage_id)
    if collision is not None and (
        collision.payload.get("update_id") != provider_update_id
        or collision.payload.get("_channel_instance") != channel_instance.model_dump()
    ):
        raise Conflict("Telegram update identity collision")
    return storage_id


def save_update(
    session,
    update: dict,
    owner_id: int,
    *,
    callback_time_known=False,
    dispatcher_version="neutral-shadow-v1",
    channel_instance=TELEGRAM_INSTANCE,
):
    if owned_message(update, owner_id) is None:
        return False
    # The legacy dispatcher cannot reconcile edits, but neutral shadow ingress
    # retains them as revisions without enqueueing a duplicate diary mutation.
    if update.get("edited_message") is not None and dispatcher_version != "neutral-shadow-v1":
        return False
    session.execute(sql_text("SELECT pg_advisory_xact_lock(72104623)"))
    received = datetime.now(UTC)
    ordering_key = _ingress_state_key("telegram:ordering", channel_instance)
    ordering = session.get(AppState, ordering_key, populate_existing=True)
    epoch = ordering.value["epoch"] if ordering else 0
    if ordering:
        previous = datetime.fromisoformat(ordering.value["last_received_at"])
    elif channel_instance == TELEGRAM_INSTANCE:
        previous = session.scalar(select(func.max(TelegramUpdate.received_at)))
    else:
        previous = None
    if previous is not None and received - previous >= timedelta(days=7):
        epoch += 1
    update = {
        **update,
        "_callback_time_known": callback_time_known,
        "_ordering_epoch": epoch,
        # This value comes from the authenticated adapter argument, overriding
        # any similarly named field supplied in the provider update.
        "_channel_instance": channel_instance.model_dump(),
    }
    update_id = _storage_update_id(session, update["update_id"], channel_instance)
    if dispatcher_version == "neutral-shadow-v1":
        try:
            neutral, _created = record_neutral_ingress(
                session,
                update,
                owner_id,
                received,
                allow_legacy_callback=True,
                channel_instance=channel_instance,
            )
        except Conflict:
            if update.get("edited_message") is not None:
                return False
            raise
        neutral.legacy_telegram_update_id = update_id
        callback = update.get("callback_query")
        action = neutral.envelope.get("action")
        if (
            callback is not None
            and action is not None
            and action["action_id"] != callback.get("data")
        ):
            update = {
                **update,
                "callback_query": {**callback, "data": action["action_id"]},
            }
        if update.get("edited_message") is not None:
            neutral.status = "processed"
            session.flush()
            return True
    inserted = session.scalar(
        insert(TelegramUpdate)
        .values(id=update_id, payload=update)
        .on_conflict_do_nothing(index_elements=[TelegramUpdate.id])
        .returning(TelegramUpdate.id)
    )
    if inserted is not None:
        upsert(
            session,
            AppState,
            {
                "key": ordering_key,
                "value": {
                    "epoch": epoch,
                    "last_received_at": received.isoformat(),
                },
            },
            ["key"],
        )
        if update.get("callback_query"):
            enqueue(
                session,
                "telegram_ack",
                {"update_id": update_id},
                f"telegram:ack:{update_id}",
                datetime.now(UTC),
            )
        message = owned_message(update, owner_id)
        command = (
            (message.get("text") or "").split(maxsplit=1)[0]
            if (message.get("text") or "").strip()
            else ""
        )
        control = command in {
            "/forget_conversation",
            "/today",
            "/status",
            "/debug",
            "/goals",
            "/pause",
            "/resume",
            "/help",
            "/start",
        }
        enqueue(
            session,
            "telegram_control" if control else "telegram_update",
            {
                "update_id": update_id,
                "provider_update_id": update["update_id"],
                "ordering_epoch": epoch,
                "channel_instance_id": f"{channel_instance.channel}:{channel_instance.instance_id}",
                "safety_checked": bool(update.get("callback_query"))
                or not (
                    message.get("voice") or (message.get("text") and not command.startswith("/"))
                ),
            },
            f"telegram:{update_id}",
            datetime.now(UTC),
        )
    return True


async def poll(
    bot: Bot,
    engine,
    settings,
    stop: asyncio.Event,
    notifications_ready=None,
    *,
    polling_request=None,
    channel_instance=TELEGRAM_INSTANCE,
):
    caught_up_at = None
    network_failures = 0
    while not stop.is_set():
        try:
            with transaction(engine) as session:
                offset_key = _ingress_state_key("telegram:offset", channel_instance)
                state = session.get(AppState, offset_key)
                offset = state.value["offset"] if state else None
                # Telegram may choose a lower random ID after a week of inactivity.
                # Use polling ingress (including non-owner updates), not diary activity.
                ingress = state.value.get("received_at") if state else None
                if ingress is None:
                    ordering = session.get(
                        AppState, _ingress_state_key("telegram:ordering", channel_instance)
                    )
                    ingress = ordering.value.get("last_received_at") if ordering else None
                if offset is not None and (
                    ingress is None
                    or datetime.now(UTC) - datetime.fromisoformat(ingress) >= timedelta(days=7)
                ):
                    offset = None
                    state.value = {"offset": None}
                    caught_up_at = None
            updates = await bot.get_updates(
                offset=offset,
                timeout=15,
                allowed_updates=["message", "edited_message", "callback_query"],
            )
            network_failures = 0
            received = datetime.now(UTC)
            time_known = caught_up_at is not None and received - caught_up_at < timedelta(
                seconds=90
            )
            for update in updates:
                with transaction(engine) as session:
                    save_update(
                        session,
                        update.to_dict(),
                        settings.telegram_user_id,
                        callback_time_known=time_known,
                        dispatcher_version=settings.telegram_dispatcher_version,
                        channel_instance=channel_instance,
                    )
                    upsert(
                        session,
                        AppState,
                        dict(
                            key=offset_key,
                            value={
                                "offset": update.update_id + 1,
                                "received_at": received.isoformat(),
                            },
                        ),
                        ["key"],
                    )
            caught_up_at = received if len(updates) < 100 else None
            if notifications_ready is not None:
                notifications_ready.set() if caught_up_at else notifications_ready.clear()
        except Exception as exc:
            if notifications_ready is not None:
                notifications_ready.clear()
            logging.getLogger("garmin_ai").warning(
                "telegram_poll_failed", extra={"error_type": type(exc).__name__}
            )
            try:
                from garmin_ai.debug import queue_error_notice

                with transaction(engine) as session:
                    queue_error_notice(session, "telegram_poll", type(exc).__name__)
            except Exception:
                pass  # A diagnostics failure must not stop message reception.
            network_failures = network_failures + 1 if isinstance(exc, NetworkError) else 0
            if network_failures >= 3 and polling_request is not None:
                # Only polling uses this transport. In-flight replies retain their
                # separate client, and the durable inbox offset is unchanged.
                try:
                    await polling_request.shutdown()
                    await polling_request.initialize()
                    network_failures = 0
                    logging.getLogger("garmin_ai").info("telegram_poll_connection_reset")
                except Exception as reset_error:
                    logging.getLogger("garmin_ai").warning(
                        "telegram_poll_reset_failed",
                        extra={"error_type": type(reset_error).__name__},
                    )
            await asyncio.sleep(5)


class DiaryDeferred(RuntimeError):
    pass


class ChannelInstanceMismatch(RuntimeError):
    pass


def process_message(engine, provider, settings, update_id: int, transcript: str | None = None):
    try:
        return _process_message(engine, provider, settings, update_id, transcript)
    except ProviderConsentRequired:
        return _process_message(engine, None, settings, update_id, transcript)
    except ChannelInstanceMismatch:
        with transaction(engine) as session:
            set_update_status(session, update_id, "invalid")
        return None
    except (ValueError, LookupError):
        response = "Не удалось применить запись или исправление. Ничего не изменено. Уточните время и детали; для отмены должна существовать предыдущая запись."
        with transaction(engine) as session:
            row = session.get(TelegramUpdate, update_id)
            raw_channel = row.payload.get("_channel_instance") if row else None
            ingress_channel = (
                ChannelInstanceRef.model_validate(raw_channel)
                if raw_channel is not None
                else ChannelInstanceRef(channel="telegram", instance_id="primary")
            )
            upsert(
                session,
                AppState,
                dict(
                    key=f"telegram:reply:{update_id}",
                    value={
                        "text": response,
                        "status": "pending",
                        "share_requirements": {},
                        "channel_instance_id": (
                            f"{ingress_channel.channel}:{ingress_channel.instance_id}"
                        ),
                    },
                ),
                ["key"],
            )
            if row:
                set_update_status(session, update_id, "invalid")
        return response


def _process_message(engine, provider, settings, update_id: int, transcript: str | None = None):
    now = datetime.now(UTC)
    actor = f"telegram:{settings.telegram_user_id}"
    with Session(engine, expire_on_commit=False) as session:
        writer_guard(session)
        from garmin_ai.accounts import effective_owner_settings

        settings = effective_owner_settings(session, settings)
        session.info["conversation_now"] = now
        from garmin_ai.integrations import channel_instance_id, configured_instance

        telegram_instance = configured_instance(settings, "channel", "telegram")
        model_instance = configured_instance(settings, "model", "gemini")
        configured_channel = ChannelInstanceRef(
            channel="telegram", instance_id=channel_instance_id(telegram_instance)
        )
        session.info["model_provider_instance_id"] = (
            model_instance.id if model_instance is not None else "model:gemini:primary"
        )
        row = session.get(TelegramUpdate, update_id)
        if row is None:
            raise LookupError("Telegram update missing")
        raw_channel = row.payload.get("_channel_instance")
        # Rows queued before the authenticated namespace was persisted belong
        # to the original primary installation only.
        ingress_channel = (
            ChannelInstanceRef.model_validate(raw_channel)
            if raw_channel is not None
            else ChannelInstanceRef(channel="telegram", instance_id="primary")
        )
        if ingress_channel != configured_channel:
            raise ChannelInstanceMismatch("Telegram update belongs to another channel instance")
        session.info["channel_instance"] = ingress_channel
        session.info["channel_destination_instance_id"] = (
            f"{ingress_channel.channel}:{ingress_channel.instance_id}"
        )
        existing = session.get(AppState, f"telegram:reply:{update_id}")
        if existing:
            return existing.value["text"]
        message = owned_message(row.payload, settings.telegram_user_id)
        if message is None:
            raise ValueError("Telegram owner mismatch")
        sent = message.get("date") if not row.payload.get("callback_query") else None
        if isinstance(sent, (int, float)):
            now = datetime.fromtimestamp(sent, UTC)
        elif isinstance(sent, str):
            now = datetime.fromisoformat(sent)
        else:
            now = row.received_at
        text = (
            "\n".join(part for part in (transcript, message.get("caption")) if part)
            if transcript is not None
            else message.get("text", "")
        )
        from garmin_ai.diary_forms import (
            check_form_safety,
            form_safety_notice,
            interpret_form,
            obvious_urgent_symptoms,
            urgent_notice,
        )

        command_name = text.split(maxsplit=1)[0] if text.strip() else ""
        callback = row.payload.get("callback_query", {}).get("data")
        pack = callback_pack(callback)
        if pack is not None:
            from garmin_ai.scenario_packs import pack_enabled

            if not pack_enabled(session, pack):
                raise ValueError("Scenario pack is disabled")
        from garmin_ai.conversation import is_analytic_reply

        analytic_reply = is_analytic_reply(
            session, message.get("reply_to_message", {}).get("message_id")
        )
        pending_form = pending_clarification(session, now)
        if (
            pending_form
            and pending_form.value.get("channel_instance_id", "telegram:primary")
            != session.info["channel_destination_instance_id"]
        ):
            pending_form = None
        form_button = pending_form.value.get("button") if pending_form else None
        tracker_pending = bool(pending_form and pending_form.value.get("definition_version_id"))
        local_form = (
            interpret_form(
                session,
                text,
                settings,
                now,
                source="telegram_voice" if transcript is not None else "telegram_text",
            )
            if (
                not analytic_reply
                and not callback
                and not command_name.startswith("/")
                and not tracker_pending
            )
            else None
        )
        if (
            local_form is not None
            and form_button == "coffee_preset"
            and transcript
            and message.get("caption")
        ):
            alternatives = [
                interpret_form(session, part, settings, now, source="telegram_voice")
                for part in (transcript, message["caption"])
            ]
            valid = [
                candidate for candidate in alternatives if candidate and candidate.intent == "log"
            ]
            if valid and len({candidate.events[0].start for candidate in valid}) == 1:
                local_form = valid[0]
        if provider is not None and local_form is not None:
            if (transcript is not None and form_button not in {"coffee", "coffee_preset"}) or (
                form_button == "coffee" and local_form.intent == "clarify"
            ):
                local_form = None
        # Screen tracker text locally first. The model safety screen may see it
        # only when the selected tracker permits sharing with that model instance.
        if local_form is not None:
            form_safety = check_form_safety(session, provider, text, update_id)
        elif tracker_pending and obvious_urgent_symptoms(text):
            form_safety = "urgent"
        elif tracker_pending and pending_form.value.get("chat_form"):
            form_safety = "unavailable"
        elif tracker_pending:
            from garmin_ai.models import EventDefinitionVersion
            from garmin_ai.share_policy import version_sharing_allowed

            version_id = UUID(pending_form.value["definition_version_id"])
            version = session.get(EventDefinitionVersion, version_id)
            categories = {"schema", "facts"}
            if version is not None and version.privacy == "sensitive":
                categories.add("original_text")
            form_safety = (
                check_form_safety(session, provider, text, update_id)
                if provider is not None
                and version_sharing_allowed(
                    session,
                    version_id,
                    destination_kind="model",
                    destination_instance_id=session.info["model_provider_instance_id"],
                    categories=categories,
                )
                else "unavailable"
            )
        else:
            form_safety = None
        if local_form is not None:
            writer_guard(session)
        earlier = session.scalar(
            select(Job.id)
            .join(
                TelegramUpdate,
                TelegramUpdate.id == cast(Job.payload["update_id"].astext, BigInteger),
            )
            .where(
                TelegramUpdate.status == "pending",
                Job.kind == "telegram_update",
                Job.status.in_(["pending", "running"]),
                func.coalesce(Job.payload["channel_instance_id"].astext, "telegram:primary")
                == session.info["channel_destination_instance_id"],
                telegram_order()
                < tuple_(row.payload.get("_ordering_epoch", 0), row.payload["update_id"]),
            )
            .limit(1)
        )
        from garmin_ai.provider_gate import paused as provider_paused

        offline_form = bool(
            (
                local_form is not None
                or callback in {"note", "medication", "coffee", "coffee:unspecified", "alcohol"}
                or (callback and callback.startswith("c:"))
            )
            and provider_paused(session, settings=settings)
        )
        if (
            earlier
            and not offline_form
            and command_name
            not in {
                "/forget_conversation",
                "/today",
                "/status",
                "/debug",
                "/goals",
                "/pause",
                "/resume",
                "/help",
                "/start",
            }
        ):
            urgent = form_safety == "urgent"
            if (
                provider
                and text.strip()
                and not command_name.startswith("/")
                and not callback
                and local_form is None
                and not tracker_pending
            ):
                if message.get("reply_to_message", {}).get("message_id") is not None:
                    from garmin_ai.agent import screen_reply_safety

                    checked = screen_reply_safety(provider, text, session.commit)
                else:
                    checked = interpret(
                        session,
                        provider,
                        text,
                        settings,
                        now,
                        source="telegram_voice" if transcript is not None else "telegram_text",
                        before_model=session.commit,
                    )
                urgent = checked.intent == "safety"
            with transaction(engine) as checked_session:
                if urgent:
                    response = urgent_notice(settings.locale)
                    upsert(
                        checked_session,
                        AppState,
                        dict(
                            key=f"telegram:reply:{update_id}",
                            value={
                                "text": response,
                                "status": "pending",
                                "channel_instance_id": session.info[
                                    "channel_destination_instance_id"
                                ],
                                "share_requirements": {},
                            },
                        ),
                        ["key"],
                    )
                    set_update_status(checked_session, update_id, "processed")
                else:
                    queued = checked_session.scalar(
                        select(Job).where(Job.dedup_key == f"telegram:{update_id}")
                    )
                    queued.payload = {**queued.payload, "safety_checked": True}
            if urgent:
                return response
            raise DiaryDeferred("Earlier diary mutation has not finished")
        if callback:
            response = handle_button(
                session,
                callback,
                settings,
                actor,
                update_id,
                now,
                time_known=bool(row.payload.get("_callback_time_known")),
            )
        elif command_name == "/start" or command_name == "/help":
            response = (
                "Готов вести ваш дневник и анализировать Garmin. Пишите, например: «кофе в 11» или «как я восстановился?»\n\n"
                "/today — последние показатели\n/status — состояние синхронизации\n/history — записи дневника\n/goals — личные цели\n/undo — отменить последнее изменение\n/cancel — отменить уточнение\n/pause — отключить вопросы\n/resume — включить вопросы\n\n"
                "Текст, голос и необходимые выдержки для ответа обрабатывает Gemini. Полная исходная история хранится локально. Наблюдения по данным не являются диагнозом."
                "\n/conversation — контекст анализа\n/forget_conversation — очистить контекст анализа"
                "\n/debug — состояние диагностики; /debug on и /debug off — уведомления об ошибках"
            )
        elif command_name == "/debug":
            from garmin_ai.debug import KEY, enabled

            parts = text.strip().split()
            if len(parts) == 2 and parts[1] in {"on", "off"}:
                message_at = int(now.timestamp())
                provider_update_id = row.payload["update_id"]
                ordering_epoch = row.payload.get("_ordering_epoch", 0)
                statement = insert(AppState).values(
                    key=KEY,
                    value={
                        "enabled": parts[1] == "on",
                        "update_id": provider_update_id,
                        "ordering_epoch": ordering_epoch,
                        "message_at": message_at,
                    },
                )
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[AppState.key],
                        set_={"value": statement.excluded.value},
                        where=tuple_(
                            func.coalesce(AppState.value["message_at"].as_integer(), -1),
                            func.coalesce(AppState.value["ordering_epoch"].as_integer(), 0),
                            func.coalesce(AppState.value["update_id"].as_integer(), -1),
                        )
                        < tuple_(message_at, ordering_epoch, provider_update_id),
                    )
                )
                session.flush()
            if len(parts) > 2 or (len(parts) == 2 and parts[1] not in {"on", "off"}):
                response = "Используйте /debug, /debug on или /debug off."
            else:
                response = (
                    "Диагностика включена. Буду сообщать об ошибках, объединяя повторяющиеся уведомления. Тексты сообщений, показатели и секреты в уведомления не попадают."
                    if enabled(session)
                    else "Диагностика выключена. Включить уведомления об ошибках: /debug on"
                )
            current_debug = session.get(AppState, KEY, populate_existing=True)
            debug_value = current_debug.value if current_debug else {}
            session.info["debug_generation"] = [
                debug_value.get("message_at"),
                debug_value.get("update_id"),
            ]
        elif command_name == "/goals":
            if len(text.split()) == 1:
                earlier_goals = session.scalar(
                    select(Job.id)
                    .join(
                        TelegramUpdate,
                        TelegramUpdate.id == cast(Job.payload["update_id"].astext, BigInteger),
                    )
                    .where(
                        Job.kind == "telegram_control",
                        Job.status.in_(["pending", "running"]),
                        TelegramUpdate.status == "pending",
                        func.coalesce(Job.payload["channel_instance_id"].astext, "telegram:primary")
                        == session.info["channel_destination_instance_id"],
                        TelegramUpdate.payload["message"]["text"].astext.op("~")(
                            "^/goals[[:space:]]+[^[:space:]]"
                        ),
                        telegram_order()
                        < tuple_(row.payload.get("_ordering_epoch", 0), row.payload["update_id"]),
                    )
                    .limit(1)
                )
                if earlier_goals:
                    raise DiaryDeferred("Earlier goal selection has not finished")
            from garmin_ai.personal_goals import telegram_goals

            response = telegram_goals(
                session,
                text,
                session.info["conversation_now"],
                sent_at=now,
                received_at=row.received_at,
                update_id=update_id,
            )
        elif command_name == "/conversation":
            from garmin_ai.conversation import conversation_summary

            response = conversation_summary(session, now)
        elif command_name == "/forget_conversation":
            from garmin_ai.conversation import forget_conversation

            forget_conversation(session)
            response = "Контекст аналитического разговора очищен. Записи дневника сохранены."
        elif command_name == "/today":
            from garmin_ai.replay import REPLAY_NOTICE, replay_generation, replay_pending_condition

            session.execute(sql_text("SELECT pg_advisory_xact_lock_shared(72104619)"))
            session.info["analysis_projection"] = {"generation": replay_generation(session)}
            replay_pending = bool(session.scalar(select(replay_pending_condition())))
            day = session.scalar(select(HealthDay).order_by(HealthDay.day.desc()).limit(1))
            if replay_pending:
                response = REPLAY_NOTICE
            elif day:
                fields = [
                    ("Сон", day.sleep_score),
                    ("HRV", day.hrv_nightly_avg),
                    ("Пульс покоя", day.resting_hr),
                    ("Body Battery максимум", day.body_battery_high),
                    ("Готовность", day.training_readiness_score),
                ]
                response = f"Последние сохранённые показатели за {day.day:%d.%m.%Y}:\n" + "\n".join(
                    f"{name}: {value if value is not None else 'нет данных'}"
                    for name, value in fields
                )
            else:
                response = "Показатели Garmin ещё не загружены."
        elif command_name == "/status":
            from garmin_ai.integration import connection_status_text
            from garmin_ai.replay import replay_generation

            session.execute(sql_text("SELECT pg_advisory_xact_lock_shared(72104619)"))
            session.info["analysis_projection"] = {"generation": replay_generation(session)}
            fresh = data_freshness(session)
            response = f"Связь с базой работает. Сохранено дней: {session.scalar(select(func.count()).select_from(HealthDay))}. Обновляемых источников: {len(fresh['endpoints'])}."
            response += "\n" + connection_status_text(fresh.get("connection", {}))
            successes = [
                v["success_at"] for v in fresh["endpoints"].values() if v.get("success_at")
            ]
            if successes:
                latest = max(successes)
                response += (
                    " Последний успешный ответ Garmin: "
                    + datetime.fromisoformat(latest)
                    .astimezone(ZoneInfo(settings.timezone))
                    .strftime("%d.%m %H:%M")
                    + "."
                )
            if not fresh["archive_replay"]["ready"]:
                response += "\nПересчёт архива не завершён; анализ Garmin временно недоступен."
            else:
                hr = fresh["channels"]["heart_rate_bpm"]
                lag = hr["observation_lag_seconds"]
                response += "\nПульс часов: " + (
                    f"последнее измерение {lag / 3600:.1f} ч назад."
                    if lag is not None
                    else "нет сохранённых измерений."
                )
                if not hr["usable_for_current_state"]:
                    response += " Данных недостаточно для оценки текущего состояния."
                if hr["coverage_ratio"] is not None:
                    response += (
                        f" Покрытие дня без заполнения пропусков: {hr['coverage_ratio']:.0%}."
                    )
                hrv = fresh["channels"]["hrv_nightly_avg"]
                response += "\nНочной HRV: " + (
                    f"сводка за {hrv['source_calendar_date']}."
                    if hrv["source_calendar_date"]
                    else "нет данных."
                )
        elif command_name == "/history":
            from garmin_ai.telegram_history import history_page

            response = history_page(session, session.info["conversation_now"])
        elif command_name == "/cancel":
            pending = session.get(AppState, pending_key(session))
            if pending:
                session.delete(pending)
            response = "Уточнение отменено. Можно добавить новую запись."
        elif command_name == "/undo":
            undo_last(session, actor=actor)
            pending = session.get(AppState, pending_key(session))
            if pending:
                session.delete(pending)
            response = (
                f"Последняя операция отменена: записей {session.info['undo_count']}."
                if session.info.get("undo_count", 1) > 1
                else "Последнее изменение отменено."
            )
        elif command_name == "/pause" or command_name == "/resume":
            enabled = command_name == "/resume"
            # Message time also handles Telegram choosing a fresh update ID after inactivity.
            message_at = int(now.timestamp())
            provider_update_id = row.payload["update_id"]
            ordering_epoch = row.payload.get("_ordering_epoch", 0)
            statement = insert(AppState).values(
                key="proactive:enabled",
                value={
                    "enabled": enabled,
                    "update_id": provider_update_id,
                    "ordering_epoch": ordering_epoch,
                    "message_at": message_at,
                },
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AppState.key],
                    set_={"value": statement.excluded.value},
                    where=tuple_(
                        func.coalesce(AppState.value["message_at"].as_integer(), -1),
                        func.coalesce(AppState.value["ordering_epoch"].as_integer(), 0),
                        func.coalesce(AppState.value["update_id"].as_integer(), -1),
                    )
                    < tuple_(message_at, ordering_epoch, provider_update_id),
                )
            )
            enabled = session.get(AppState, "proactive:enabled", populate_existing=True).value[
                "enabled"
            ]
            response = (
                f"Настройка сохранена: вопросы разрешены. Лимит в день: {settings.question_budget}, только вне тихих часов."
                if enabled
                else "Вопросы отключены. Синхронизация продолжается."
            )
        elif (
            message.get("voice")
            and provider is None
            and not transcript
            and not (local_form is not None and message.get("caption"))
            and not (tracker_pending and message.get("caption"))
        ):
            response = "Распознавание голосовых сообщений недоступно: Gemini не подключён. Показатели доступны через /today, записи — через кнопки."
        elif command_name.startswith("/") and not (tracker_pending and command_name == "/skip"):
            response = "Неизвестная команда. Доступные команды: /help."
        elif not text.strip():
            response = "Пришлите текст или голосовое сообщение."
        elif form_safety == "urgent" and (local_form is not None or tracker_pending):
            response = urgent_notice(settings.locale)
        elif local_form is not None:
            response = apply_command(
                session, local_form, text=text, update_id=update_id, actor=actor, now=now
            )
            if form_safety == "unavailable":
                response += "\n\n" + form_safety_notice(settings.locale)
        elif (
            tracker_pending
            and not analytic_reply
            and (not command_name.startswith("/") or command_name == "/skip")
        ):
            from garmin_ai.natural_language import process_tracker_text
            from garmin_ai.share_policy import version_sharing_allowed

            version_id = UUID(pending_form.value["definition_version_id"])
            if not version_sharing_allowed(
                session,
                version_id,
                destination_kind="channel",
                destination_instance_id=session.info["channel_destination_instance_id"],
                categories={"schema"},
            ):
                session.delete(pending_form)
                response = "Доступ к трекеру изменился. Откройте актуальное меню."
            else:
                from garmin_ai.share_policy import track_channel_share
                from garmin_ai.tracker_chat_form import advance_chat_form, begin_chat_form
                from garmin_ai.tracker_forms import FormSpec

                track_channel_share(session, version_id, {"schema"})
                if pending_form.value.get("chat_form"):
                    outcome = advance_chat_form(
                        session,
                        pending_form,
                        text,
                        actor=actor,
                        now=now,
                        source="telegram_voice" if transcript is not None else "telegram_text",
                    )
                    if outcome.get("written") or outcome.get("cancelled"):
                        session.delete(pending_form)
                    response = outcome["response"]
                else:
                    result = process_tracker_text(
                        session,
                        provider,
                        {
                            "text": text,
                            "operation_id": f"telegram:{update_id}",
                            "selected_definition_version_id": pending_form.value[
                                "definition_version_id"
                            ],
                        },
                        granted={"read:diary", "write:diary"},
                        actor=actor,
                        now=now,
                        timezone=settings.timezone,
                        locale=settings.locale,
                        source="telegram_voice" if transcript is not None else "telegram_text",
                    )
                    if result.get("written"):
                        session.delete(pending_form)
                        response = "Запись сохранена."
                    elif result["intent"] == "deterministic_form":
                        form = next(
                            (
                                FormSpec.model_validate(item)
                                for item in result["forms"]
                                if item["action"]["definition_version_id"] == str(version_id)
                            ),
                            None,
                        )
                        response = (
                            begin_chat_form(
                                pending_form,
                                form,
                                timezone=settings.timezone,
                                locale=settings.locale,
                            )
                            if form is not None
                            else "Форма трекера недоступна. Откройте актуальное меню."
                        )
                    else:
                        if version_sharing_allowed(
                            session,
                            version_id,
                            destination_kind="channel",
                            destination_instance_id=session.info["channel_destination_instance_id"],
                            categories={"facts"},
                        ):
                            track_channel_share(session, version_id, {"facts"})
                            response = (
                                result.get("clarification") or "Уточните значения для записи."
                            )
                        else:
                            response = "Уточните значения для записи."
            if form_safety == "unavailable":
                response += "\n\n" + form_safety_notice(settings.locale)
        elif provider is not None and analytic_reply:
            response = answer_question(
                session,
                provider,
                text,
                settings,
                now,
                before_model=session.commit,
                update_id=update_id,
                reply_to_message_id=message["reply_to_message"]["message_id"],
            )
        elif provider is None:
            response = "Обработка свободного текста пока недоступна. Записи можно добавить кнопками, показатели посмотреть через /today."
        else:
            budget = AnalysisBudget()
            command = interpret(
                session,
                provider,
                text,
                settings,
                now,
                source="telegram_voice" if transcript is not None else "telegram_text",
                before_model=session.commit,
                budget=budget,
            )
            writer_guard(session)
            if command._dismiss_refinement:
                pending = session.get(AppState, pending_key(session))
                if pending:
                    session.delete(pending)
            if command.intent == "safety":
                response = (
                    command.clarification
                    or "При внезапных тяжёлых симптомах нужна срочная медицинская помощь: позвоните 112 или в местную экстренную службу. Не ждите оценки по данным часов."
                )
            elif command.intent == "question":
                response = answer_question(
                    session,
                    provider,
                    text,
                    settings,
                    now,
                    before_model=session.commit,
                    update_id=update_id,
                    reply_to_message_id=message.get("reply_to_message", {}).get("message_id"),
                    budget=budget,
                )
            else:
                response = apply_command(
                    session, command, text=text, update_id=update_id, actor=actor, now=now
                )
        from garmin_ai.proactive import reconcile_answers

        reconcile_answers(session, datetime.now(UTC))
        writer_guard(session)
        upsert(
            session,
            AppState,
            dict(
                key=f"telegram:reply:{update_id}",
                value={
                    "text": response,
                    "status": "pending",
                    "kind": "analysis" if session.info.get("analysis_reply") else "diary",
                    "analysis_epoch": session.info.get("analysis_epoch"),
                    "analysis_projection": session.info.get("analysis_projection"),
                    "goals_revision": session.info.get("goals_revision"),
                    "debug_generation": session.info.get("debug_generation"),
                    "keyboard": session.info.get("reply_keyboard", True),
                    "channel_instance_id": session.info["channel_destination_instance_id"],
                    "share_requirements": session.info.get("channel_share_requirements", {}),
                },
            ),
            ["key"],
        )
        row = session.get(TelegramUpdate, update_id, populate_existing=True)
        if row is None:
            raise LookupError("Telegram update missing after interpretation")
        set_update_status(session, update_id, "processed")
        session.commit()
        return response


def handle_button(session, callback, settings, actor, update_id, now, *, time_known=True):
    if callback.startswith("h:"):
        from garmin_ai.telegram_history import selected_action

        return selected_action(session, callback, now, actor)
    if callback.startswith("create:"):
        from garmin_ai.accounts import owner
        from garmin_ai.events import Conflict
        from garmin_ai.share_policy import version_sharing_allowed
        from garmin_ai.tracker_forms import form_for_action

        locale = (
            getattr(settings, "locale", None) or session.info.get("locale") or owner(session).locale
        )
        try:
            form = form_for_action(session, callback, locale=locale)
        except (Conflict, LookupError):
            return "Этот трекер изменён или удалён. Откройте актуальное меню и выберите его снова."
        if not version_sharing_allowed(
            session,
            form.action.definition_version_id,
            destination_kind="channel",
            destination_instance_id=session.info.get("channel_destination_instance_id", ""),
            categories={"schema"},
        ):
            return "Этот трекер больше недоступен в Telegram. Откройте актуальное меню."
        if not session.info.get("channel_destination_instance_id"):
            return "Этот трекер больше недоступен в Telegram. Откройте актуальное меню."
        from garmin_ai.share_policy import track_channel_share
        from garmin_ai.tracker_chat_form import begin_chat_form

        track_channel_share(session, form.action.definition_version_id, {"schema"})
        upsert(
            session,
            AppState,
            {
                "key": pending_key(session),
                "value": {
                    "text": f"Заполнить трекер «{form.title}»",
                    "question": "",
                    "event_ids": [],
                    "action": "log",
                    "button": "tracker_form",
                    "definition_version_id": str(form.action.definition_version_id),
                    "channel_instance_id": session.info["channel_destination_instance_id"],
                    "created_at": session.info.get("conversation_now", now).isoformat(),
                },
            },
            ["key"],
        )
        pending = session.get(AppState, pending_key(session), populate_existing=True)
        question = begin_chat_form(
            pending,
            form,
            timezone=getattr(settings, "timezone", None) or owner(session).timezone,
            locale=locale,
        )
        pending.value = {**pending.value, "question": question}
        from garmin_ai.diary_forms import form_safety_notice

        return question + "\n\n" + form_safety_notice(locale)
    previous = session.get(AppState, pending_key(session))
    if previous:
        session.delete(previous)
        session.flush()

    def follow_up(response, event_id=None, preset_recipe=None):
        upsert(
            session,
            AppState,
            dict(
                key=pending_key(session),
                value={
                    "text": "Уточнение уже сохранённой записи"
                    if event_id
                    else "Добавить лекарство"
                    if callback == "medication"
                    else {
                        "coffee": "Добавить кофе; время неизвестно",
                        "coffee_preset": "Добавить выбранный кофе; время неизвестно",
                        "migraine": "Добавить начало мигрени; время неизвестно",
                        "alcohol": "Добавить алкоголь; время неизвестно",
                    }.get(callback, "Добавить заметку"),
                    "question": response,
                    "event_ids": [str(event_id)] if event_id else [],
                    "action": "close" if callback == "end" else "update" if event_id else "log",
                    "button": callback,
                    "pack": callback_pack(callback),
                    "channel_instance_id": session.info.get(
                        "channel_destination_instance_id", "telegram:primary"
                    ),
                    **(
                        {
                            "preset_recipe": preset_recipe,
                            "preset_selected_at": session.info.get(
                                "conversation_now", now
                            ).isoformat(),
                        }
                        if preset_recipe is not None
                        else {}
                    ),
                    "optional_refinement": bool(
                        event_id and callback in {"coffee", "migraine", "alcohol"}
                    ),
                    "created_at": session.info.get("conversation_now", now).isoformat(),
                },
            ),
            ["key"],
        )
        return response

    if callback == "coffee" and settings.caffeine_presets:
        from garmin_ai.caffeine_presets import keyboard

        session.info["reply_keyboard"] = keyboard(settings.caffeine_presets)
        return "Выберите напиток."
    if callback.startswith("c:"):
        from garmin_ai.caffeine_presets import callback as preset_callback
        from garmin_ai.caffeine_presets import keyboard, label

        preset = next(
            (item for item in settings.caffeine_presets if preset_callback(item) == callback), None
        )
        if preset is None:
            session.info["reply_keyboard"] = keyboard(settings.caffeine_presets)
            return "Пресет изменён или удалён. Выберите напиток заново; запись ещё не сохранена."
        if not time_known:
            from garmin_ai.diary_forms import PROMPTS

            callback = "coffee_preset"
            return follow_up(
                "Выбран " + label(preset) + ". Запись ещё не сохранена. " + PROMPTS[callback],
                preset_recipe=preset.recipe.model_dump(mode="json"),
            )
        event = EventInput(
            start=now,
            timezone=settings.timezone,
            source="telegram_button",
            payload=preset.recipe.model_copy(deep=True),
        )
        recorded = create_event(
            session, event, actor=actor, idempotency_key=f"telegram:{update_id}:button"
        )
        callback = "coffee"
        return follow_up(
            "Записал сейчас: " + label(preset) + ". Можно уточнить сообщением.", recorded.id
        )
    if callback == "coffee:unspecified":
        callback = "coffee"
    if callback in {"medication", "note"}:
        from garmin_ai.diary_forms import PROMPTS

        return follow_up(PROMPTS[callback])
    if callback == "end":
        active = session.scalars(
            select(Event).where(
                Event.kind == "migraine",
                Event.status == "confirmed",
                Event.deleted.is_(False),
                or_(Event.end.is_(None), Event.end > now),
                Event.start <= now,
            )
        ).all()
        if not active:
            return "Открытой мигрени нет. Сначала сообщите, когда она началась, или отметьте начало кнопкой 🤕."
        if len(active) != 1:
            from garmin_ai.telegram_history import history_page

            response = history_page(
                session, session.info.get("conversation_now", now), open_only=True
            )
            upsert(
                session,
                AppState,
                {
                    "key": pending_key(session),
                    "value": {
                        "text": "Отметить окончание мигрени",
                        "question": "Выберите эпизод и время окончания.",
                        "event_ids": [str(e.id) for e in active[:20]],
                        "targets_complete": len(active) <= 20,
                        "action": "close",
                        "button": "end",
                        "channel_instance_id": session.info.get(
                            "channel_destination_instance_id", "telegram:primary"
                        ),
                        "created_at": session.info.get("conversation_now", now).isoformat(),
                    },
                },
                ["key"],
            )
            return response
        row = active[0]
        data = {k: v for k, v in serialize(row).items() if k in EventInput.model_fields}
        if not time_known:
            return follow_up(
                "Время нажатия кнопки неизвестно. Во сколько закончилась мигрень?", row.id
            )
        data["end"] = now
        event = EventInput.model_validate(data)
        update_event(session, row.id, event, revision=row.revision, actor=actor)
        return "Отметил завершение мигрени сейчас."
    payloads = {
        "coffee": {"type": "caffeine", "beverage": "кофе, тип не указан"},
        "migraine": {"type": "migraine"},
        "alcohol": {"type": "alcohol", "description": "Алкоголь, количество не указано"},
    }
    if callback not in payloads:
        raise ValueError("Unknown callback")
    if not time_known:
        return follow_up(
            "Время нажатия кнопки неизвестно. Укажите дату и время события; запись ещё не сохранена."
        )
    event = EventInput(
        start=now, timezone=settings.timezone, source="telegram_button", payload=payloads[callback]
    )
    recorded = create_event(
        session, event, actor=actor, idempotency_key=f"telegram:{update_id}:button"
    )
    response = {
        "coffee": "Записал кофе сейчас. Тип и количество можно уточнить сообщением.",
        "migraine": "Записал начало мигрени сейчас. Можете добавить силу боли от 0 до 10 и наличие ауры.",
        "alcohol": "Записал алкоголь сейчас. Вид и количество можно уточнить сообщением.",
    }[callback]
    return follow_up(response, recorded.id)


class DeliveryUncertain(RuntimeError):
    pass


async def deliver(
    bot: Bot,
    engine,
    owner_id: int,
    key: str,
    text: str,
    keyboard=False,
    *,
    channel_instance: ChannelInstanceRef | None = TELEGRAM_INSTANCE,
):
    with transaction(engine) as session:
        reply = (
            session.get(AppState, "telegram:reply:" + key.removeprefix("update:"))
            if key.startswith("update:")
            else None
        )
        projection = reply.value.get("analysis_projection") if reply else None
        legacy_analysis = bool(
            reply
            and reply.value.get("kind") == "analysis"
            and "analysis_projection" not in reply.value
        )
    if projection is None and not legacy_analysis:
        return await _deliver_with_consent_fence(
            bot, engine, owner_id, key, text, keyboard, channel_instance=channel_instance
        )
    from garmin_ai.replay import REPLAY_NOTICE, replay_generation, replay_pending_condition

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as guard:
        if not guard.scalar(sql_text("SELECT pg_try_advisory_lock_shared(72104619)")):
            raise DiaryDeferred("Analysis delivery awaits normalization")
        try:
            with transaction(engine) as session:
                generation = replay_generation(session)
                if (
                    generation is not None
                    if legacy_analysis
                    else generation != projection.get("generation")
                ) or session.scalar(select(replay_pending_condition())):
                    text = REPLAY_NOTICE
                    key = key + ":replay-notice"
            return await _deliver_with_consent_fence(
                bot,
                engine,
                owner_id,
                key,
                text,
                keyboard,
                channel_instance=channel_instance,
                reply_key=key.removesuffix(":replay-notice"),
            )
        finally:
            guard.execute(sql_text("SELECT pg_advisory_unlock_shared(72104619)"))


def _reply_share_allowed(session, reply, channel_instance):
    """Recheck current consent for all custom material captured in a queued reply."""
    if reply is None:
        return True
    value = reply.value
    bound_channel = value.get("channel_instance_id")
    if bound_channel is not None and (
        channel_instance is None
        or bound_channel != f"{channel_instance.channel}:{channel_instance.instance_id}"
    ):
        return False
    requirements = value.get("share_requirements")
    if requirements is None:
        # Pre-upgrade replies did not record their dependencies. Suppress them
        # when a sensitive tracker exists because their text cannot be audited.
        return (
            session.scalar(
                select(EventDefinitionVersion.id)
                .join(EventDefinition, EventDefinitionVersion.definition_id == EventDefinition.id)
                .where(
                    EventDefinition.namespace == "user",
                    EventDefinitionVersion.privacy == "sensitive",
                )
                .limit(1)
            )
            is None
        )
    if not requirements:
        return True
    if channel_instance is None or value.get("channel_instance_id") != (
        f"{channel_instance.channel}:{channel_instance.instance_id}"
    ):
        return False
    from garmin_ai.share_policy import version_sharing_allowed

    return all(
        version_sharing_allowed(
            session,
            UUID(raw),
            destination_kind="channel",
            destination_instance_id=value["channel_instance_id"],
            categories=set(categories),
        )
        for raw, categories in requirements.items()
    )


async def _deliver_with_consent_fence(
    bot,
    engine,
    owner_id,
    key,
    text,
    keyboard=False,
    *,
    channel_instance=None,
    reply_key=None,
):
    from garmin_ai.share_policy import channel_consent_delivery_fence

    with channel_consent_delivery_fence(engine):
        return await _deliver(
            bot,
            engine,
            owner_id,
            key,
            text,
            keyboard,
            channel_instance=channel_instance,
            reply_key=reply_key,
        )


async def _deliver(
    bot: Bot,
    engine,
    owner_id: int,
    key: str,
    text: str,
    keyboard=False,
    *,
    channel_instance: ChannelInstanceRef | None = None,
    reply_key: str | None = None,
):
    # Telegram has no idempotency key for sendMessage. An ambiguous send is not
    # retried automatically, preventing duplicate proactive questions.
    reply_key = reply_key or key
    with transaction(engine) as session:
        if channel_instance is not None:
            session.info["channel_instance"] = channel_instance
            session.info["channel_destination_instance_id"] = (
                f"{channel_instance.channel}:{channel_instance.instance_id}"
            )
        existing = session.scalars(
            select(AppState).where(AppState.key.startswith(f"outbox:{key}:"))
        ).all()
        legacy = any(not row.value.get("formatted") for row in existing)
        reply = (
            session.get(AppState, "telegram:reply:" + reply_key.removeprefix("update:"))
            if reply_key.startswith("update:")
            else None
        )
        destination_id = (
            f"{channel_instance.channel}:{channel_instance.instance_id}"
            if channel_instance is not None
            else reply.value.get("channel_instance_id", "telegram:primary")
            if reply is not None
            else "telegram:primary"
        )
        if not _reply_share_allowed(session, reply, channel_instance):
            logging.getLogger("garmin_ai").info(
                "telegram_reply_blocked", extra={"reason": "channel_consent_changed"}
            )
            return
        reply_epoch = reply.value.get("analysis_epoch") if reply else None
        debug_generation = reply.value.get("debug_generation") if reply else None
        goals_revision = reply.value.get("goals_revision") if reply else None
        reply_kind = (
            reply.value.get("kind", "diary")
            if reply
            else (
                "analysis"
                if any(row.value.get("kind") == "analysis" for row in existing)
                else "diary"
            )
        )
    parts = (
        [(text[i : i + 3500], []) for i in range(0, len(text), 3500)]
        if legacy
        else message_parts(text)
    )
    delivery_started = any(row.value.get("status") == "sent" for row in existing)
    for part_index, (part, entities) in enumerate(parts):
        with ExitStack() as guards:
            if not delivery_started and goals_revision is not None:
                from garmin_ai.personal_goals import delivery_guard

                if not guards.enter_context(delivery_guard(engine, goals_revision)):
                    return
            index = part_index * 3500
            part_key = f"outbox:{key}:{index}"
            with transaction(engine) as session:
                current_reply = (
                    session.get(
                        AppState,
                        "telegram:reply:" + reply_key.removeprefix("update:"),
                        populate_existing=True,
                    )
                    if reply_key.startswith("update:")
                    else None
                )
                if not _reply_share_allowed(session, current_reply, channel_instance):
                    logging.getLogger("garmin_ai").info(
                        "telegram_reply_blocked", extra={"reason": "channel_consent_changed"}
                    )
                    return
                if channel_instance is not None:
                    session.info["channel_destination_instance_id"] = (
                        f"{channel_instance.channel}:{channel_instance.instance_id}"
                    )
                if keyboard is True and index == 0:
                    default_keyboard = scenario_keyboard(session)
                else:
                    default_keyboard = KEYBOARD
                if debug_generation is not None:
                    current_debug = session.get(AppState, "telegram:debug")
                    debug_value = current_debug.value if current_debug else {}
                    if debug_generation != [
                        debug_value.get("message_at"),
                        debug_value.get("update_id"),
                    ]:
                        return
                if reply_kind == "analysis":
                    from garmin_ai.conversation import epoch_matches
                    from garmin_ai.personal_goals import revision_matches

                    if (
                        not delivery_started
                        and goals_revision is not None
                        and not revision_matches(session, goals_revision)
                    ):
                        return

                    current_reply = session.get(
                        AppState, "telegram:reply:" + reply_key.removeprefix("update:")
                    )
                    if (
                        current_reply and current_reply.value.get("status") == "forgotten"
                    ) or not epoch_matches(session, reply_epoch):
                        return
                previous = session.get(AppState, part_key)
                if previous and previous.value["status"] == "sent":
                    delivery_started = True
                    continue
                if previous and previous.value.get("retry_at"):
                    remaining = (
                        datetime.fromisoformat(previous.value["retry_at"]) - datetime.now(UTC)
                    ).total_seconds()
                    if remaining > 0:
                        raise RetryAfter(int(remaining) + 1)
                if previous and previous.value["status"] in {"sending", "uncertain"}:
                    raise DeliveryUncertain("Prior Telegram send has unknown outcome")
                if keyboard and index == 0:
                    from garmin_ai.telegram_history import renew_selectors

                    renew_selectors(session, keyboard, datetime.now(UTC))
                upsert(
                    session,
                    AppState,
                    dict(
                        key=part_key,
                        value={
                            "status": "sending",
                            "started_at": datetime.now(UTC).isoformat(),
                            "formatted": not legacy,
                            "channel_instance_id": destination_id,
                        },
                    ),
                    ["key"],
                )
            try:
                message = await bot.send_message(
                    chat_id=owner_id,
                    text=part,
                    entities=entities,
                    parse_mode=None,
                    reply_markup=(
                        InlineKeyboardMarkup.de_json(keyboard, None)
                        if isinstance(keyboard, dict)
                        else default_keyboard
                    )
                    if keyboard and index == 0
                    else None,
                )
            except RetryAfter as exc:
                seconds = (
                    exc.retry_after.total_seconds()
                    if isinstance(exc.retry_after, timedelta)
                    else exc.retry_after
                )
                with transaction(engine) as session:
                    upsert(
                        session,
                        AppState,
                        dict(
                            key=part_key,
                            value={
                                "status": "pending",
                                "formatted": not legacy,
                                "channel_instance_id": destination_id,
                                "retry_at": (
                                    datetime.now(UTC) + timedelta(seconds=seconds)
                                ).isoformat(),
                            },
                        ),
                        ["key"],
                    )
                raise
            except Exception:
                with transaction(engine) as session:
                    upsert(
                        session,
                        AppState,
                        dict(
                            key=part_key,
                            value={
                                "status": "uncertain",
                                "formatted": not legacy,
                                "channel_instance_id": destination_id,
                            },
                        ),
                        ["key"],
                    )
                raise DeliveryUncertain("Telegram delivery could not be confirmed") from None
            with transaction(engine) as session:
                if keyboard and index == 0:
                    renew_selectors(session, keyboard, datetime.now(UTC), delivered=True)
                upsert(
                    session,
                    AppState,
                    dict(
                        key=part_key,
                        value={
                            "status": "sent",
                            "message_id": message.message_id,
                            "kind": reply_kind,
                            "formatted": not legacy,
                            "channel_instance_id": destination_id,
                        },
                    ),
                    ["key"],
                )
            delivery_started = True


def reconcile_failed_inbox(session):
    for row in session.scalars(
        select(TelegramUpdate)
        .join(Job, TelegramUpdate.id == cast(Job.payload["update_id"].astext, BigInteger))
        .where(
            TelegramUpdate.status.in_(["pending", "processed", "invalid"]),
            Job.kind.in_(["telegram_update", "telegram_control"]),
            Job.status == "failed",
        )
    ):
        reply = session.get(AppState, f"telegram:reply:{row.id}")
        parts = session.scalars(
            select(AppState).where(AppState.key.like(f"outbox:update:{row.id}:%"))
        ).all()
        legacy = any(not part.value.get("formatted") for part in parts)
        expected = 0
        if reply:
            expected = (
                (len(reply.value["text"]) + 3499) // 3500
                if legacy
                else len(message_parts(reply.value["text"]))
            )
        if (
            reply
            and sum(part.value.get("status") == "sent" for part in parts) < expected
            and not any(part.value.get("status") in {"uncertain", "sending"} for part in parts)
        ):
            job = session.scalar(select(Job).where(Job.dedup_key == f"telegram:{row.id}"))
            job.status, job.attempts = "pending", 0
            job.run_at = max(
                [
                    datetime.now(UTC) + timedelta(seconds=30),
                    *[
                        datetime.fromisoformat(part.value["retry_at"])
                        for part in parts
                        if part.value.get("retry_at")
                    ],
                ]
            )
            continue
        set_update_status(session, row.id, "failed")
        if not session.get(AppState, f"telegram:reply:{row.id}") and not session.get(
            AppState, f"outbox:update:{row.id}:0"
        ):
            enqueue(
                session,
                "telegram_failure",
                {"update_id": row.id},
                f"telegram:failure:{row.id}",
                datetime.now(UTC),
            )
