"""Private Telegram inbox, durable replies, and a shared diary/analysis agent."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter

from garmin_ai.agent import answer_question, apply_command, interpret
from garmin_ai.db import transaction
from garmin_ai.events import EventInput, create_event, serialize, undo_last, update_event
from garmin_ai.jobs import enqueue
from garmin_ai.models import AppState, Event, HealthDay, Job, TelegramUpdate
from garmin_ai.normalize import upsert
from garmin_ai.queries import data_freshness

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
    if event.kind == "caffeine":
        return f"Кофе: {payload['beverage']}, порций: {payload.get('servings', 1)}"
    if event.kind == "migraine":
        severity = payload.get("severity")
        return (
            "Мигрень"
            + (f", {severity}/10" if severity is not None else "")
            + (", завершена" if event.end else ", время окончания не указано")
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


def save_update(session, update: dict, owner_id: int):
    if owned_message(update, owner_id) is None:
        return False
    update_id = update["update_id"]
    inserted = session.scalar(
        insert(TelegramUpdate)
        .values(id=update_id, payload=update)
        .on_conflict_do_nothing(index_elements=[TelegramUpdate.id])
        .returning(TelegramUpdate.id)
    )
    if inserted is not None:
        message = owned_message(update, owner_id)
        command = (
            (message.get("text") or "").split(maxsplit=1)[0]
            if (message.get("text") or "").strip()
            else ""
        )
        control = command in {
            "/today",
            "/status",
            "/history",
            "/pause",
            "/resume",
            "/help",
            "/start",
        }
        enqueue(
            session,
            "telegram_control" if control else "telegram_update",
            {"update_id": update_id},
            f"telegram:{update_id}",
            datetime.now(UTC),
        )
    return True


async def poll(bot: Bot, engine, settings, stop: asyncio.Event):
    while not stop.is_set():
        try:
            with transaction(engine) as session:
                state = session.get(AppState, "telegram:offset")
                offset = state.value["offset"] if state else None
            updates = await bot.get_updates(
                offset=offset, timeout=15, allowed_updates=["message", "callback_query"]
            )
            for update in updates:
                with transaction(engine) as session:
                    save_update(session, update.to_dict(), settings.telegram_user_id)
                    upsert(
                        session,
                        AppState,
                        dict(key="telegram:offset", value={"offset": update.update_id + 1}),
                        ["key"],
                    )
        except Exception as exc:
            logging.getLogger("garmin_ai").warning(
                "telegram_poll_failed", extra={"error_type": type(exc).__name__}
            )
            await asyncio.sleep(5)


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
        session.info["timezone"] = settings.timezone
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
        text = transcript if transcript is not None else message.get("text", "")
        command_name = text.split(maxsplit=1)[0] if text.strip() else ""
        callback = row.payload.get("callback_query", {}).get("data")
        if callback:
            response = handle_button(session, callback, settings, actor, update_id, now)
        elif command_name == "/start" or command_name == "/help":
            response = (
                "Готов вести ваш дневник и анализировать Garmin. Пишите, например: «кофе в 11» или «как я восстановился?»\n\n"
                "/today — последние показатели\n/status — состояние синхронизации\n/history — записи дневника\n/undo — отменить последнее изменение\n/pause — отключить вопросы\n/resume — включить вопросы\n\n"
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
            fresh = data_freshness(session)
            response = f"Связь с базой работает. Сохранено дней: {session.scalar(select(func.count()).select_from(HealthDay))}. Обновляемых источников: {len(fresh['endpoints'])}."
            if fresh["endpoints"]:
                latest = max(v["success_at"] for v in fresh["endpoints"].values())
                response += (
                    " Последняя успешная загрузка: "
                    + datetime.fromisoformat(latest)
                    .astimezone(ZoneInfo(settings.timezone))
                    .strftime("%d.%m %H:%M")
                    + "."
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
        elif command_name == "/undo":
            undo_last(session, actor=actor)
            pending = session.get(AppState, "conversation:pending")
            if pending:
                session.delete(pending)
            response = "Последнее изменение отменено."
        elif command_name == "/pause" or command_name == "/resume":
            enabled = command_name == "/resume"
            upsert(
                session,
                AppState,
                dict(key="proactive:enabled", value={"enabled": enabled}),
                ["key"],
            )
            response = (
                "Вопросы включены; не больше двух в день и только вне тихих часов."
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
            if command.intent == "safety":
                response = "При внезапных тяжёлых симптомах нужна срочная медицинская помощь: позвоните 112 или в местную экстренную службу. Не ждите оценки по данным часов."
            elif command.intent == "question":
                response = answer_question(
                    session, provider, text, settings, now, before_model=session.commit
                )
            else:
                response = apply_command(
                    session, command, text=text, update_id=update_id, actor=actor, now=now
                )
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


def handle_button(session, callback, settings, actor, update_id, now):
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
                    else "Добавить заметку",
                    "question": response,
                    "event_ids": [str(event_id)] if event_id else [],
                    "action": "update" if event_id else "log",
                    "button": callback,
                    "created_at": now.isoformat(),
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
                Event.kind == "migraine", Event.deleted.is_(False), Event.end.is_(None)
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
                        "created_at": now.isoformat(),
                    },
                ),
                ["key"],
            )
            return question
        row = active[0]
        data = {k: v for k, v in serialize(row).items() if k in EventInput.model_fields}
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
    for index in range(0, len(text), 3500):
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
                    value={"status": "sending", "started_at": datetime.now(UTC).isoformat()},
                ),
                ["key"],
            )
        try:
            message = await bot.send_message(
                chat_id=owner_id,
                text=text[index : index + 3500],
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
                    session, AppState, dict(key=part_key, value={"status": "uncertain"}), ["key"]
                )
            raise DeliveryUncertain("Telegram delivery could not be confirmed") from None
        with transaction(engine) as session:
            upsert(
                session,
                AppState,
                dict(key=part_key, value={"status": "sent", "message_id": message.message_id}),
                ["key"],
            )


def reconcile_failed_inbox(session):
    for row in session.scalars(
        select(TelegramUpdate)
        .join(Job, TelegramUpdate.id == cast(Job.payload["update_id"].astext, BigInteger))
        .where(
            TelegramUpdate.status == "pending",
            Job.kind.in_(["telegram_update", "telegram_control"]),
            Job.status == "failed",
        )
    ):
        row.status = "failed"
