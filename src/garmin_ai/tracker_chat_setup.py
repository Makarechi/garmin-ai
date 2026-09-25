"""Explicit, durable tracker setup for a paired Telegram owner."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select

from garmin_ai.accounts import owner
from garmin_ai.events import Conflict
from garmin_ai.i18n import normalized_locale
from garmin_ai.models import AppState, ChannelBinding
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    definition_spec,
    preview_tracker,
)

_BOUNDS = re.compile(r"^(\d+)\s*[-–]\s*(\d+)$")
_SETUP_IDLE_LIMIT = timedelta(hours=24)


def _english(locale: str) -> bool:
    return normalized_locale(locale) != "ru"


def _say(locale: str, ru: str, en: str) -> str:
    return en if _english(locale) else ru


def _key(session) -> str:
    return "tracker:chat-setup:" + session.info["channel_destination_instance_id"]


def _paired_owner(session, sender_id: int) -> bool:
    destination = session.info["channel_instance"]
    return (
        session.scalar(
            select(ChannelBinding.id).where(
                ChannelBinding.owner_id == owner(session).id,
                ChannelBinding.channel == destination.channel,
                ChannelBinding.channel_instance_id == destination.instance_id,
                ChannelBinding.external_id == str(sender_id),
            )
        )
        is not None
    )


def active_setup_row(session):
    row = session.get(AppState, _key(session), populate_existing=True)
    if row is None:
        return None
    stamp = row.value.get("last_activity_at")
    try:
        activity = datetime.fromisoformat(stamp) if stamp else row.updated_at
    except (TypeError, ValueError):
        activity = None
    now = session.info.get("conversation_now", datetime.now(UTC))
    if activity is None or activity.utcoffset() is None or activity < now - _SETUP_IDLE_LIMIT:
        session.delete(row)
        session.flush()
        return None
    return row


def active_setup(session) -> bool:
    return active_setup_row(session) is not None


def start_setup(session, *, sender_id: int, locale: str, timezone: str) -> str:
    if not _paired_owner(session, sender_id):
        return _say(
            locale,
            "Для создания трекера нужен подтверждённый доступ владельца к этому каналу.",
            "Tracker setup requires a confirmed owner binding for this channel.",
        )
    existing = active_setup_row(session)
    if existing is not None:
        existing.value = {
            **existing.value,
            "last_activity_at": session.info.get("conversation_now", datetime.now(UTC)).isoformat(),
        }
        return _say(
            locale,
            "Черновик уже открыт. Пришлите ответ, /preview или /cancel.",
            "A draft is already open. Reply, use /preview or /cancel.",
        )
    state = {
        "key": "chat_" + uuid4().hex[:16],
        "name": None,
        "fields": [],
        "locale": locale,
        "timezone": timezone,
        "privacy": "private",
        "confirmation_token": None,
        "last_activity_at": session.info.get("conversation_now", datetime.now(UTC)).isoformat(),
    }
    session.add(AppState(key=_key(session), value=state))
    return _say(locale, "Как назвать новый трекер?", "What should the new tracker be called?")


def _field(text: str) -> TrackerFieldDraft:
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 2 or not parts[0]:
        raise ValueError("field syntax")
    label, specification = parts
    words = specification.split(maxsplit=1)
    if not words:
        raise ValueError("field syntax")
    kind = words[0].casefold()
    argument = words[1].strip() if len(words) == 2 else ""
    common = {"key": "field", "label": label}
    if kind in {"текст", "text"} and not argument:
        return TrackerFieldDraft(**common, kind="text")
    if kind in {"да/нет", "yes/no", "boolean"} and not argument:
        return TrackerFieldDraft(**common, kind="boolean")
    if kind in {"выбор", "choice"}:
        options = [item.strip() for item in argument.split(",")]
        return TrackerFieldDraft(**common, kind="choice", options=options)
    if kind in {"шкала", "scale", "счётчик", "счетчик", "count"}:
        match = _BOUNDS.fullmatch(argument)
        if match is None:
            raise ValueError("numeric bounds")
        minimum, maximum = (int(value) for value in match.groups())
        if kind in {"шкала", "scale"}:
            if len(f"score_{minimum}-{maximum}") > 32:
                raise ValueError("scale unit exceeds supported length")
            return TrackerFieldDraft(**common, kind="scale", minimum=minimum, maximum=maximum)
        return TrackerFieldDraft(
            **common,
            kind="integer",
            unit="count",
            minimum=minimum,
            maximum=maximum,
            metric_semantics="event_count",
        )
    raise ValueError("field type")


def _field_help(locale: str) -> str:
    return _say(
        locale,
        "Добавьте поле: «Оценка | шкала 1-5», «Количество | счётчик 0-100», "
        "«Заметка | текст», «Есть симптом | да/нет» или «Тип | выбор A, B». "
        "Затем /preview. Последнее поле можно убрать через /remove_field.",
        "Add a field: 'Rating | scale 1-5', 'Count | count 0-100', "
        "'Note | text', 'Present | yes/no' or 'Type | choice A, B'. "
        "Then use /preview. Use /remove_field to remove the last field.",
    )


def _literal(value) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!<>|~-])", r"\\\1", str(value))


def _field_preview(field: dict) -> str:
    details = [field["kind"]]
    if field.get("minimum") is not None and field.get("maximum") is not None:
        details.append(f"{field['minimum']}–{field['maximum']}")
    if field.get("unit"):
        details.append(_literal(field["unit"]))
    if field.get("options"):
        details.append(", ".join(_literal(option) for option in field["options"]))
    return f"{_literal(field['label'])} | {' '.join(details)}"


def advance_setup(session, text: str, *, sender_id: int, actor: str, locale: str) -> str:
    row = active_setup_row(session)
    if row is None:
        raise LookupError("Tracker setup draft missing")
    state = deepcopy(row.value)
    locale = state["locale"]
    answer = text.strip()
    if answer == "/cancel":
        session.delete(row)
        return _say(locale, "Черновик удалён.", "Draft discarded.")
    if not _paired_owner(session, sender_id):
        return _say(
            locale,
            "Для создания трекера нужен подтверждённый доступ владельца к этому каналу.",
            "Tracker setup requires a confirmed owner binding for this channel.",
        )
    state["last_activity_at"] = session.info.get("conversation_now", datetime.now(UTC)).isoformat()
    row.value = deepcopy(state)
    if answer.startswith("/privacy "):
        privacy = answer.partition(" ")[2].strip().casefold()
        if privacy not in {"private", "sensitive"}:
            return _say(
                locale,
                "Используйте /privacy private или /privacy sensitive.",
                "Use /privacy private or /privacy sensitive.",
            )
        state["privacy"] = privacy
        state["confirmation_token"] = None
        row.value = state
        return _say(locale, "Приватность черновика обновлена.", "Draft privacy updated.")
    if not state["name"]:
        if not 1 <= len(answer) <= 64 or answer.startswith("/"):
            return _say(
                locale,
                "Название должно быть от 1 до 64 символов.",
                "Name must be 1–64 characters.",
            )
        state["name"] = answer
        row.value = state
        return _field_help(locale)
    if answer == "/remove_field":
        if not state["fields"]:
            return _field_help(locale)
        state["fields"].pop()
        state["confirmation_token"] = None
        row.value = state
        return _say(locale, "Последнее поле удалено.", "Last field removed.")
    if answer == "/preview":
        if not state["fields"]:
            return _field_help(locale)
        draft = _draft(state)
        try:
            preview = preview_tracker(session, draft)
        except ValueError:
            return _say(
                locale,
                "Схема трекера слишком велика. Удалите поле командой /remove_field.",
                "Tracker schema is too large. Remove a field with /remove_field.",
            )
        state["confirmation_token"] = preview["confirmation_token"]
        row.value = state
        lines = [_field_preview(field) for field in state["fields"]]
        sensitive_notice = (
            "\n"
            + _say(
                locale,
                "После создания чувствительный трекер будет недоступен в Telegram, пока вы отдельно не разрешите этому каналу доступ к схеме и фактам в настройках согласия.",
                "After creation, this sensitive tracker will be unavailable in Telegram until you separately allow this channel access to its schema and facts in consent settings.",
            )
            if state["privacy"] == "sensitive"
            else ""
        )
        return (
            _say(locale, "Предпросмотр", "Preview")
            + f": {_literal(state['name'])}\n"
            + "\n".join(lines)
            + "\n"
            + _say(
                locale,
                f"Приватность: {state['privacy']}. Подтвердите командой /confirm_tracker или продолжите редактирование.",
                f"Privacy: {state['privacy']}. Use /confirm_tracker to create it, or keep editing.",
            )
            + sensitive_notice
        )
    if answer == "/confirm_tracker":
        if not state["confirmation_token"]:
            return _say(locale, "Сначала откройте /preview.", "Use /preview first.")
        try:
            created = confirm_tracker(
                session,
                TrackerConfirmation(
                    draft=_draft(state), confirmation_token=state["confirmation_token"]
                ),
                actor=actor,
            )
        except Conflict:
            state["confirmation_token"] = None
            row.value = state
            return _say(
                locale,
                "Предпросмотр устарел. Откройте /preview снова.",
                "Preview expired. Use /preview again.",
            )
        session.delete(row)
        return (
            _say(locale, "Трекер создан", "Tracker created")
            + f": {_literal(created['tracker']['shortcut'])}"
        )
    try:
        field = _field(answer)
        field = field.model_copy(update={"key": f"field_{len(state['fields']) + 1}"})
        if len(state["fields"]) >= 32:
            raise ValueError("field limit")
        state["fields"].append(field.model_dump(mode="json"))
        state["confirmation_token"] = None
        definition_spec(_draft(state))
    except (ValueError, ValidationError, OverflowError):
        return _field_help(locale)
    row.value = state
    return _say(
        locale,
        "Поле добавлено. Добавьте ещё или откройте /preview.",
        "Field added. Add another or use /preview.",
    )


def _draft(state: dict) -> TrackerSetupDraft:
    return TrackerSetupDraft(
        key=state["key"],
        name=state["name"],
        locale=state["locale"],
        reminder_timezone=state["timezone"],
        privacy=state["privacy"],
        fields=[TrackerFieldDraft.model_validate(field) for field in state["fields"]],
    )
