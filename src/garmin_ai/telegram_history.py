"""Owner-only diary navigation using short-lived, revision-bound selectors."""

import secrets
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, select, tuple_

from garmin_ai.events import delete_event, event_query_allowed
from garmin_ai.models import AppState, Event, EventDefinition, EventDefinitionVersion
from garmin_ai.normalize import upsert
from garmin_ai.pending_state import pending_key

PREFIX = "telegram:selection:"


def channel_destination(session):
    """Return the authenticated ingress destination, never an implicit primary."""
    from garmin_ai.channels import ChannelInstanceRef

    channel = session.info.get("channel_instance")
    if not isinstance(channel, ChannelInstanceRef) or channel.channel != "telegram":
        return None
    return f"{channel.channel}:{channel.instance_id}"


def button(session, now, label, action, *, event=None, cursor=None, open_only=False):
    token = secrets.token_urlsafe(12)
    session.add(
        AppState(
            key=PREFIX + token,
            value={
                "action": action,
                "delivered": False,
                "event_id": str(event.id) if event else None,
                "revision": event.revision if event else None,
                "cursor": cursor,
                "open_only": open_only,
                "channel_instance_id": channel_destination(session),
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
            },
        )
    )
    return {"text": label, "callback_data": "h:" + token}


def history_page(session, now, *, cursor=None, open_only=False):
    from garmin_ai.diary_labels import diary_label

    pending = session.get(AppState, pending_key(session))
    if pending and pending.value.get("action") in {"update", "close"}:
        session.delete(pending)
        session.flush()
    session.execute(
        delete(AppState).where(
            AppState.key.startswith(PREFIX),
            AppState.value["expires_at"].as_string() < now.isoformat(),
            or_(
                AppState.value["delivered"].as_boolean().is_(True),
                AppState.value["expires_at"].as_string() < (now - timedelta(days=7)).isoformat(),
            ),
        )
    )
    from garmin_ai.share_policy import event_sharing_filter

    destination = channel_destination(session)
    query = select(Event).where(
        Event.deleted.is_(False),
        event_query_allowed(),
        event_sharing_filter(
            destination_kind="channel",
            destination_instance_id=destination,
            categories={"schema", "facts"},
        )
        if destination
        else Event.kind.not_like("user.%"),
    )
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
        version = (
            session.get(EventDefinitionVersion, event.definition_version_id)
            if event.definition_version_id
            else None
        )
        definition = session.get(EventDefinition, version.definition_id) if version else None
        custom = definition is not None and definition.namespace == "user"
        if custom and version is not None:
            from garmin_ai.share_policy import track_channel_share

            track_channel_share(session, version.id, {"schema", "facts"})
        operations = set(version.allowed_operations) if version else {"update", "delete"}
        actions = []
        if "update" in operations and (not custom or "query" in operations):
            actions.append(
                button(
                    session,
                    now,
                    f"{index}: Завершить" if open_only else f"{index}: Исправить",
                    "close" if open_only else "edit",
                    event=event,
                )
            )
        if "delete" in operations:
            actions.append(button(session, now, f"{index}: Удалить", "delete", event=event))
        if actions:
            keyboard.append(actions)
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
    now = session.info.get("conversation_now", now)
    destination = channel_destination(session)
    if destination is None or value.get("channel_instance_id", "telegram:primary") != destination:
        return "Эта кнопка устарела. Откройте /history заново."
    if value["action"] == "page":
        return history_page(
            session, now, cursor=value.get("cursor"), open_only=value.get("open_only", False)
        )
    event = session.get(Event, UUID(value["event_id"]), populate_existing=True)
    if event is None or event.deleted or event.revision != value["revision"]:
        return "Запись уже изменилась. Откройте /history и выберите её снова."
    if event.kind.startswith("user."):
        from garmin_ai.share_policy import version_sharing_allowed

        if event.definition_version_id is None or not version_sharing_allowed(
            session,
            event.definition_version_id,
            destination_kind="channel",
            destination_instance_id=destination,
            categories={"schema", "facts"},
        ):
            return "Доступ к этой записи изменился. Откройте /history заново."
        from garmin_ai.share_policy import track_channel_share

        track_channel_share(session, event.definition_version_id, {"schema", "facts"})
        if value["action"] == "edit":
            from garmin_ai.events import Conflict
            from garmin_ai.tracker_chat_form import FormAnswerError, begin_chat_form
            from garmin_ai.tracker_forms import action_for_event, form_for_action

            try:
                action = action_for_event(
                    session, event.id, locale=session.info.get("locale", "ru")
                )
                form = form_for_action(session, action.id, locale=session.info.get("locale", "ru"))
            except (Conflict, LookupError):
                return "Запись изменилась. Откройте /history снова."
            upsert(
                session,
                AppState,
                {
                    "key": pending_key(session),
                    "value": {
                        "action": "update",
                        "event_ids": [str(event.id)],
                        "button": "tracker_form",
                        "definition_version_id": str(event.definition_version_id),
                        "channel_instance_id": destination,
                        "created_at": now.isoformat(),
                    },
                },
                ["key"],
            )
            pending_form = session.get(AppState, pending_key(session), populate_existing=True)
            try:
                return begin_chat_form(
                    pending_form,
                    form,
                    timezone=event.timezone,
                    locale=session.info.get("locale", "ru"),
                )
            except FormAnswerError as exc:
                session.delete(pending_form)
                session.flush()
                return str(exc)
    if value["action"] == "delete":
        delete_event(session, event.id, revision=value["revision"], actor=actor)
        pending = session.get(AppState, pending_key(session), populate_existing=True)
        if pending and str(event.id) in pending.value.get("event_ids", []):
            session.delete(pending)
        return "Запись удалена. Отменить последнее изменение: /undo."
    back_button = button(
        session, now, "Это не оно — история", "page", open_only=value["action"] == "close"
    )
    pending = {
        "text": "Исправить выбранную запись",
        "question": "Во сколько закончилась мигрень?"
        if value["action"] == "close"
        else "Что исправить?",
        "event_ids": [str(event.id)],
        "selection_revision": event.revision,
        "selection_prompt": back_button["callback_data"],
        "selected_at": now.isoformat(),
        "explicit_selector": True,
        "targets_complete": True,
        "selection_expires_at": (now + timedelta(minutes=15)).isoformat(),
        "action": "close" if value["action"] == "close" else "update",
        "created_at": now.isoformat(),
        "channel_instance_id": destination,
    }
    upsert(session, AppState, {"key": pending_key(session), "value": pending}, ["key"])
    session.info["reply_keyboard"] = {"inline_keyboard": [[back_button]]}
    from garmin_ai.diary_labels import diary_label

    return (
        f"Выбрано: {event.start.astimezone(ZoneInfo(event.timezone)):%d.%m.%Y %H:%M} — {diary_label(event)[:200]}. "
        + (
            "Во сколько закончилась мигрень?"
            if value["action"] == "close"
            else "Напишите, что исправить."
        )
    )


def renew_selectors(session, keyboard, now, *, delivered=False):
    """Activate durable reply selectors around the actual first delivery attempt."""
    if not isinstance(keyboard, dict):
        return
    for buttons in keyboard.get("inline_keyboard", []):
        for item in buttons:
            callback = item.get("callback_data", "")
            if not callback.startswith("h:"):
                continue
            row = session.get(AppState, PREFIX + callback[2:], populate_existing=True)
            if row:
                row.value = {
                    **row.value,
                    "expires_at": (now + timedelta(minutes=15)).isoformat(),
                    "delivered": delivered,
                }
                pending = session.get(AppState, pending_key(session), populate_existing=True)
                if pending and pending.value.get("selection_prompt") == callback:
                    pending.value = {
                        **pending.value,
                        "created_at": now.isoformat(),
                        "selection_expires_at": (now + timedelta(minutes=15)).isoformat(),
                    }
