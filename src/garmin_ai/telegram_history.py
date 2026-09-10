"""Owner-only diary navigation using short-lived, revision-bound selectors."""

import secrets
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, tuple_

from garmin_ai.events import delete_event
from garmin_ai.models import AppState, Event
from garmin_ai.normalize import upsert

PREFIX = "telegram:selection:"


def button(session, now, label, action, *, event=None, cursor=None, open_only=False):
    token = secrets.token_urlsafe(12)
    session.add(
        AppState(
            key=PREFIX + token,
            value={
                "action": action,
                "event_id": str(event.id) if event else None,
                "revision": event.revision if event else None,
                "cursor": cursor,
                "open_only": open_only,
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
            },
        )
    )
    return {"text": label, "callback_data": "h:" + token}


def history_page(session, now, *, cursor=None, open_only=False):
    from garmin_ai.telegram import diary_label

    pending = session.get(AppState, "conversation:pending")
    if pending:
        session.delete(pending)
        session.flush()
    session.execute(
        delete(AppState).where(
            AppState.key.startswith(PREFIX),
            AppState.value["expires_at"].as_string() < now.isoformat(),
        )
    )
    query = select(Event).where(Event.deleted.is_(False))
    if open_only:
        query = query.where(
            Event.kind == "migraine",
            Event.status == "confirmed",
            Event.start <= now,
            (Event.end.is_(None) | (Event.end > now)),
        )
    if cursor:
        query = query.where(
            tuple_(Event.start, Event.id)
            < tuple_(datetime.fromisoformat(cursor[0]), UUID(cursor[1]))
        )
    rows = session.scalars(query.order_by(Event.start.desc(), Event.id.desc()).limit(11)).all()
    keyboard, lines = [], []
    for index, event in enumerate(rows[:10], 1):
        title = f"{index}. {event.start.astimezone(ZoneInfo(event.timezone)):%d.%m.%Y %H:%M} — {diary_label(event)[:160]}"
        lines.append(title)
        keyboard.append(
            [
                button(
                    session,
                    now,
                    f"{index}: Завершить" if open_only else f"{index}: Исправить",
                    "close" if open_only else "edit",
                    event=event,
                ),
                button(session, now, f"{index}: Удалить", "delete", event=event),
            ]
        )
    navigation = [button(session, now, "Сначала", "page", open_only=open_only)]
    if len(rows) > 10:
        last = rows[9]
        navigation.append(
            button(
                session,
                now,
                "Далее",
                "page",
                cursor=[last.start.isoformat(), str(last.id)],
                open_only=open_only,
            )
        )
    keyboard.append(navigation)
    session.info["reply_keyboard"] = {"inline_keyboard": keyboard}
    return ("Выберите эпизод:\n" if open_only else "История дневника:\n") + (
        "\n".join(lines) if lines else "Записей нет."
    )


def selected_action(session, callback, now, actor):
    token = callback.removeprefix("h:")
    row = (
        session.get(AppState, PREFIX + token, populate_existing=True) if len(token) <= 32 else None
    )
    if row is None or datetime.fromisoformat(row.value["expires_at"]) <= now:
        return "Эта кнопка устарела. Откройте /history заново."
    value = row.value
    if value["action"] == "page":
        return history_page(
            session, now, cursor=value.get("cursor"), open_only=value.get("open_only", False)
        )
    event = session.get(Event, UUID(value["event_id"]), populate_existing=True)
    if event is None or event.deleted or event.revision != value["revision"]:
        return "Запись уже изменилась. Откройте /history и выберите её снова."
    if value["action"] == "delete":
        delete_event(session, event.id, revision=value["revision"], actor=actor)
        return "Запись удалена. Отменить последнее изменение: /undo."
    pending = {
        "text": "Исправить выбранную запись",
        "question": "Что исправить?",
        "event_ids": [str(event.id)],
        "selection_revision": event.revision,
        "explicit_selector": True,
        "targets_complete": True,
        "selection_expires_at": (now + timedelta(minutes=15)).isoformat(),
        "action": "close" if value["action"] == "close" else "update",
        "created_at": now.isoformat(),
    }
    upsert(session, AppState, {"key": "conversation:pending", "value": pending}, ["key"])
    session.info["reply_keyboard"] = {
        "inline_keyboard": [[button(session, now, "Это не оно — история", "page")]]
    }
    from garmin_ai.telegram import diary_label

    return (
        f"Выбрано: {event.start.astimezone(ZoneInfo(event.timezone)):%d.%m.%Y %H:%M} — {diary_label(event)[:200]}. "
        + (
            "Во сколько закончилась мигрень?"
            if value["action"] == "close"
            else "Напишите, что исправить."
        )
    )
