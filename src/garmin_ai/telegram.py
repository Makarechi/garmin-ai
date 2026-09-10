"""Private Telegram inbox, durable replies, and a shared diary/analysis agent."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, cast, func, or_, select, tuple_
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter

from garmin_ai.agent import answer_question, apply_command, interpret
from garmin_ai.db import transaction, writer_guard
from garmin_ai.events import EventInput, create_event, serialize, undo_last, update_event
from garmin_ai.jobs import enqueue, telegram_order
from garmin_ai.models import AppState, Event, HealthDay, Job, TelegramUpdate
from garmin_ai.normalize import upsert
from garmin_ai.queries import data_freshness
from garmin_ai.telegram_format import message_parts

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


def diary_label(event):
    payload = event.payload
    if event.kind == "headache_observation":
        from garmin_ai.events import headache_observation_label

        return headache_observation_label(payload)
    if event.kind == "caffeine":
        from garmin_ai.events import caffeine_total

        total = caffeine_total(payload)
        label = f"Кофе: {payload['beverage']}, порций: {payload.get('servings', 1)}"
        if total["min"] is not None and total["max"] is not None:
            label += f"; всего кофеина {total['min']:g}–{total['max']:g} мг"
        elif total["estimate"] is not None:
            qualifier = "около " if total["provenance"] != "reported_label" else ""
            label += f"; всего кофеина {qualifier}{total['estimate']:g} мг"
        elif total["min"] is not None:
            label += f"; всего кофеина не менее {total['min']:g} мг"
        elif total["max"] is not None:
            label += f"; всего кофеина не более {total['max']:g} мг"
        else:
            return label + "; суммарная доза неизвестна"
        return label + (
            " (по этикетке)"
            if total["provenance"] == "reported_label"
            else " (оценка)"
            if total["provenance"] == "estimated"
            else " (источник дозы не указан)"
        )
    if event.kind == "migraine":
        severity = payload.get("severity")
        return (
            "Мигрень"
            + (f", {severity}/10" if severity is not None else "")
            + (
                ", завершена"
                if event.end and event.end <= datetime.now(UTC)
                else ", ещё не завершена"
            )
        )
    if event.kind == "medication":
        return f"Лекарство: {payload['name']}, {payload['dose']} {payload['unit']}"
    return payload.get("description", "Запись дневника")


def owned_message(update: dict, owner_id: int):
    if owner_id <= 0:
        return None
    callback = update.get("callback_query")
    message = callback.get("message", {}) if callback else update.get("message", {})
    sender = callback.get("from", {}) if callback else message.get("from", {})
    chat = message.get("chat", {})
    if sender.get("id") != owner_id or chat.get("id") != owner_id or chat.get("type") != "private":
        return None
    return message


def save_update(session, update: dict, owner_id: int, *, callback_time_known=False):
    if owned_message(update, owner_id) is None:
        return False
    session.execute(sql_text("SELECT pg_advisory_xact_lock(72104623)"))
    received = datetime.now(UTC)
    ordering = session.get(AppState, "telegram:ordering", populate_existing=True)
    epoch = ordering.value["epoch"] if ordering else 0
    previous = (
        datetime.fromisoformat(ordering.value["last_received_at"])
        if ordering
        else session.scalar(select(func.max(TelegramUpdate.received_at)))
    )
    if previous is not None and received - previous >= timedelta(days=7):
        epoch += 1
    update = {**update, "_callback_time_known": callback_time_known, "_ordering_epoch": epoch}
    update_id = update["update_id"]
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
                "key": "telegram:ordering",
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
            "/today",
            "/status",
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
                "ordering_epoch": epoch,
                "safety_checked": bool(update.get("callback_query"))
                or not (
                    message.get("voice") or (message.get("text") and not command.startswith("/"))
                ),
            },
            f"telegram:{update_id}",
            datetime.now(UTC),
        )
    return True


async def poll(bot: Bot, engine, settings, stop: asyncio.Event, notifications_ready=None):
    caught_up_at = None
    while not stop.is_set():
        try:
            with transaction(engine) as session:
                state = session.get(AppState, "telegram:offset")
                offset = state.value["offset"] if state else None
                # Telegram may choose a lower random ID after a week of inactivity.
                # Use polling ingress (including non-owner updates), not diary activity.
                ingress = state.value.get("received_at") if state else None
                if ingress is None:
                    ordering = session.get(AppState, "telegram:ordering")
                    ingress = ordering.value.get("last_received_at") if ordering else None
                if offset is not None and (
                    ingress is None
                    or datetime.now(UTC) - datetime.fromisoformat(ingress) >= timedelta(days=7)
                ):
                    offset = None
                    state.value = {"offset": None}
                    caught_up_at = None
            updates = await bot.get_updates(
                offset=offset, timeout=15, allowed_updates=["message", "callback_query"]
            )
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
                    )
                    upsert(
                        session,
                        AppState,
                        dict(
                            key="telegram:offset",
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
            await asyncio.sleep(5)


class DiaryDeferred(RuntimeError):
    pass


def process_message(engine, provider, settings, update_id: int, transcript: str | None = None):
    try:
        return _process_message(engine, provider, settings, update_id, transcript)
    except (ValueError, LookupError):
        response = "Не удалось применить запись или исправление. Ничего не изменено. Уточните время и детали; для отмены должна существовать предыдущая запись."
        with transaction(engine) as session:
            upsert(
                session,
                AppState,
                dict(
                    key=f"telegram:reply:{update_id}", value={"text": response, "status": "pending"}
                ),
                ["key"],
            )
            row = session.get(TelegramUpdate, update_id)
            if row:
                row.status = "invalid"
        return response


def _process_message(engine, provider, settings, update_id: int, transcript: str | None = None):
    now = datetime.now(UTC)
    actor = f"telegram:{settings.telegram_user_id}"
    with Session(engine, expire_on_commit=False) as session:
        writer_guard(session)
        session.info["timezone"] = settings.timezone
        session.info["conversation_now"] = now
        existing = session.get(AppState, f"telegram:reply:{update_id}")
        if existing:
            return existing.value["text"]
        row = session.get(TelegramUpdate, update_id)
        if row is None:
            raise LookupError("Telegram update missing")
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
        command_name = text.split(maxsplit=1)[0] if text.strip() else ""
        callback = row.payload.get("callback_query", {}).get("data")
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
                telegram_order() < tuple_(row.payload.get("_ordering_epoch", 0), update_id),
            )
            .limit(1)
        )
        if earlier and command_name not in {
            "/today",
            "/status",
            "/pause",
            "/resume",
            "/help",
            "/start",
        }:
            urgent = False
            if provider and text.strip() and not command_name.startswith("/") and not callback:
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
                    response = (
                        checked.clarification
                        or "При внезапных тяжёлых симптомах нужна срочная медицинская помощь: позвоните 112 или в местную экстренную службу. Не ждите оценки по данным часов."
                    )
                    upsert(
                        checked_session,
                        AppState,
                        dict(
                            key=f"telegram:reply:{update_id}",
                            value={"text": response, "status": "pending"},
                        ),
                        ["key"],
                    )
                    checked_session.get(TelegramUpdate, update_id).status = "processed"
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
                "/today — последние показатели\n/status — состояние синхронизации\n/history — записи дневника\n/undo — отменить последнее изменение\n/cancel — отменить уточнение\n/pause — отключить вопросы\n/resume — включить вопросы\n\n"
                "Текст, голос и необходимые выдержки для ответа обрабатывает Gemini. Полная исходная история хранится локально. Наблюдения по данным не являются диагнозом."
            )
        elif command_name == "/today":
            day = session.scalar(select(HealthDay).order_by(HealthDay.day.desc()).limit(1))
            if day:
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
                response += f" Покрытие дня без заполнения пропусков: {hr['coverage_ratio']:.0%}."
            hrv = fresh["channels"]["hrv_nightly_avg"]
            response += "\nНочной HRV: " + (
                f"сводка за {hrv['source_calendar_date']}."
                if hrv["source_calendar_date"]
                else "нет данных."
            )
        elif command_name == "/history":
            events = session.scalars(
                select(Event).where(Event.deleted.is_(False)).order_by(Event.start.desc()).limit(10)
            ).all()
            response = (
                "\n".join(
                    f"{r.start.astimezone(ZoneInfo(r.timezone)):%d.%m %H:%M} — {diary_label(r)}"
                    for r in events
                )
                if events
                else "В дневнике пока нет записей."
            )
        elif command_name == "/cancel":
            pending = session.get(AppState, "conversation:pending")
            if pending:
                session.delete(pending)
            response = "Уточнение отменено. Можно добавить новую запись."
        elif command_name == "/undo":
            undo_last(session, actor=actor)
            pending = session.get(AppState, "conversation:pending")
            if pending:
                session.delete(pending)
            response = "Последнее изменение отменено."
        elif command_name == "/pause" or command_name == "/resume":
            enabled = command_name == "/resume"
            # Message time also handles Telegram choosing a fresh update ID after inactivity.
            message_at = int(now.timestamp())
            statement = insert(AppState).values(
                key="proactive:enabled",
                value={"enabled": enabled, "update_id": update_id, "message_at": message_at},
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AppState.key],
                    set_={"value": statement.excluded.value},
                    where=tuple_(
                        func.coalesce(AppState.value["message_at"].as_integer(), -1),
                        func.coalesce(AppState.value["update_id"].as_integer(), -1),
                    )
                    < tuple_(message_at, update_id),
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
        elif message.get("voice") and provider is None:
            response = "Распознавание голосовых сообщений недоступно: Gemini не подключён. Показатели доступны через /today, записи — через кнопки."
        elif command_name.startswith("/"):
            response = "Неизвестная команда. Доступные команды: /help."
        elif not text.strip():
            response = "Пришлите текст или голосовое сообщение."
        elif provider is None:
            response = "Обработка свободного текста пока недоступна. Записи можно добавить кнопками, показатели посмотреть через /today."
        else:
            command = interpret(
                session,
                provider,
                text,
                settings,
                now,
                source="telegram_voice" if transcript is not None else "telegram_text",
                before_model=session.commit,
            )
            writer_guard(session)
            if command.intent == "safety":
                response = (
                    command.clarification
                    or "При внезапных тяжёлых симптомах нужна срочная медицинская помощь: позвоните 112 или в местную экстренную службу. Не ждите оценки по данным часов."
                )
            elif command.intent == "question":
                response = answer_question(
                    session, provider, text, settings, now, before_model=session.commit
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
            dict(key=f"telegram:reply:{update_id}", value={"text": response, "status": "pending"}),
            ["key"],
        )
        row = session.get(TelegramUpdate, update_id, populate_existing=True)
        if row is None:
            raise LookupError("Telegram update missing after interpretation")
        row.status = "processed"
        session.commit()
        return response


def handle_button(session, callback, settings, actor, update_id, now, *, time_known=True):
    previous = session.get(AppState, "conversation:pending")
    if previous:
        session.delete(previous)
        session.flush()

    def follow_up(response, event_id=None):
        upsert(
            session,
            AppState,
            dict(
                key="conversation:pending",
                value={
                    "text": "Уточнение уже сохранённой записи"
                    if event_id
                    else "Добавить лекарство"
                    if callback == "medication"
                    else {
                        "coffee": "Добавить кофе; время неизвестно",
                        "migraine": "Добавить начало мигрени; время неизвестно",
                        "alcohol": "Добавить алкоголь; время неизвестно",
                    }.get(callback, "Добавить заметку"),
                    "question": response,
                    "event_ids": [str(event_id)] if event_id else [],
                    "action": "close" if callback == "end" else "update" if event_id else "log",
                    "button": callback,
                    "created_at": session.info.get("conversation_now", now).isoformat(),
                },
            ),
            ["key"],
        )
        return response

    if callback in {"medication", "note"}:
        return follow_up(
            "Напишите название лекарства, дозу и время приёма."
            if callback == "medication"
            else "Напишите заметку и время, к которому она относится."
        )
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
            question = "Уточните, какой эпизод мигрени завершился и во сколько."
            upsert(
                session,
                AppState,
                dict(
                    key="conversation:pending",
                    value={
                        "text": "Отметить окончание мигрени",
                        "question": question,
                        "event_ids": [str(e.id) for e in active[:20]],
                        "targets_complete": len(active) <= 20,
                        "action": "close",
                        "button": "end",
                        "created_at": session.info.get("conversation_now", now).isoformat(),
                    },
                ),
                ["key"],
            )
            return question
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


async def deliver(bot: Bot, engine, owner_id: int, key: str, text: str, keyboard=False):
    # Telegram has no idempotency key for sendMessage. An ambiguous send is not
    # retried automatically, preventing duplicate proactive questions.
    with transaction(engine) as session:
        existing = session.scalars(
            select(AppState).where(AppState.key.startswith(f"outbox:{key}:"))
        ).all()
        legacy = any(not row.value.get("formatted") for row in existing)
    parts = (
        [(text[i : i + 3500], []) for i in range(0, len(text), 3500)]
        if legacy
        else message_parts(text)
    )
    for part_index, (part, entities) in enumerate(parts):
        index = part_index * 3500
        part_key = f"outbox:{key}:{index}"
        with transaction(engine) as session:
            previous = session.get(AppState, part_key)
            if previous and previous.value["status"] == "sent":
                continue
            if previous and previous.value.get("retry_at"):
                remaining = (
                    datetime.fromisoformat(previous.value["retry_at"]) - datetime.now(UTC)
                ).total_seconds()
                if remaining > 0:
                    raise RetryAfter(int(remaining) + 1)
            if previous and previous.value["status"] in {"sending", "uncertain"}:
                raise DeliveryUncertain("Prior Telegram send has unknown outcome")
            upsert(
                session,
                AppState,
                dict(
                    key=part_key,
                    value={
                        "status": "sending",
                        "started_at": datetime.now(UTC).isoformat(),
                        "formatted": not legacy,
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
                reply_markup=KEYBOARD if keyboard and index == 0 else None,
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
                    dict(key=part_key, value={"status": "uncertain", "formatted": not legacy}),
                    ["key"],
                )
            raise DeliveryUncertain("Telegram delivery could not be confirmed") from None
        with transaction(engine) as session:
            upsert(
                session,
                AppState,
                dict(
                    key=part_key,
                    value={
                        "status": "sent",
                        "message_id": message.message_id,
                        "formatted": not legacy,
                    },
                ),
                ["key"],
            )


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
        row.status = "failed"
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
