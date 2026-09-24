"""Deterministic, durable Telegram entry form for generated trackers."""

import json
import math
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from garmin_ai.events import Conflict
from garmin_ai.tracker_forms import (
    FormSpec,
    FormSubmission,
    FormValidationError,
    form_for_action,
    submit_form,
)


def _message(locale: str, ru: str, en: str) -> str:
    return en if locale.split("-", 1)[0] == "en" else ru


def _steps(form: FormSpec, field_order: list[str] | None = None) -> list[str]:
    names = field_order if field_order is not None else [row.name for row in form.fields]
    return [
        "__start__",
        *(["__end__"] if form.topology != "point" else []),
        *[f"field:{name}" for name in names],
    ]


def _prompt(
    form: FormSpec,
    index: int,
    field_order: list[str] | None = None,
    *,
    locale: str = "ru",
    state: dict | None = None,
) -> str:
    step = _steps(form, field_order)[index]
    editing = state is not None and state["action_id"].startswith("edit:")
    keep = _message(
        locale,
        " Ответьте «=», чтобы оставить прежнее значение.",
        " Reply '=' to keep the current value.",
    )
    if step == "__start__":
        prompt = _message(
            locale,
            "Когда началась запись? Ответьте «сейчас» или укажите YYYY-MM-DD HH:MM.",
            "When did the entry start? Reply 'now' or enter YYYY-MM-DD HH:MM.",
        )
        return prompt + (f" {state['start']}.{keep}" if editing else "")
    if step == "__end__":
        optional = form.topology != "bounded_interval"
        prompt = _message(
            locale,
            f"Когда запись закончилась? Укажите YYYY-MM-DD HH:MM{' или «нет», если эпизод ещё идёт' if optional else ''}.",
            f"When did the entry end? Enter YYYY-MM-DD HH:MM{" or 'none' if it is still open" if optional else ''}.",
        )
        return prompt + (f" {state['end'] or '—'}.{keep}" if editing else "")
    field = next(row for row in form.fields if row.name == step.removeprefix("field:"))
    detail = f" ({field.unit})" if field.unit else ""
    if field.input == "choice":
        detail += ": " + ", ".join(str(option) for option in field.options)
    if field.minimum is not None and field.maximum is not None:
        detail += f" [{field.minimum:g}–{field.maximum:g}]"
    optional = (
        _message(locale, " Ответьте «-», чтобы пропустить.", " Reply '-' to skip.")
        if not field.required
        else ""
    )
    current = state["values"].get(field.name) if editing else None
    return f"{field.label}{detail}?{optional}" + (
        f" {current}.{keep}" if current is not None else ""
    )


def begin_chat_form(pending, form: FormSpec, *, timezone: str, locale: str) -> str:
    """Pin the schema, revision and submission identity before the first answer."""

    if form.action.kind not in {"create_entry", "edit_entry"}:
        raise ValueError("Chat form requires a tracker entry action")
    if form.action.kind == "create_entry" and form.submission_id is None:
        raise ValueError("Create form requires a submission ID")
    state = {
        "action_id": form.id,
        "schema_hash": form.schema_hash,
        "submission_id": form.submission_id,
        "timezone": form.initial_timezone or timezone,
        "locale": locale,
        "field_order": [field.name for field in form.fields],
        "step": 0,
        "start": form.initial_start.isoformat() if form.initial_start else None,
        "end": form.initial_end.isoformat() if form.initial_end else None,
        "values": dict(form.initial_values),
        "units": dict(form.initial_units),
    }
    pending.value = {**pending.value, "chat_form": state}
    return _prompt(form, 0, locale=locale, state=state)


def _time(text: str, timezone: str, now: datetime) -> datetime:
    if text.casefold() in {"сейчас", "now"}:
        return now
    parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed


def _value(text: str, field, locale: str):
    if text == "-" and not field.required:
        return None
    if field.input == "text":
        if not text or (field.max_length is not None and len(text) > field.max_length):
            raise ValueError(
                _message(
                    locale, "Укажите текст допустимой длины", "Enter text within the allowed length"
                )
            )
        return text
    if field.input == "integer":
        if not text.lstrip("-").isdigit():
            raise ValueError(_message(locale, "Нужно целое число", "Enter a whole number"))
        value = int(text)
    elif field.input == "number":
        value = float(text.replace(",", "."))
        if not math.isfinite(value):
            raise ValueError(_message(locale, "Нужно конечное число", "Enter a finite number"))
    elif field.input == "boolean":
        normalized = text.casefold()
        if normalized not in {"да", "нет", "yes", "no", "true", "false"}:
            raise ValueError(_message(locale, "Ответьте «да» или «нет»", "Reply yes or no"))
        return normalized in {"да", "yes", "true"}
    elif field.input == "choice":
        match = next(
            (option for option in field.options if str(option).casefold() == text.casefold()), None
        )
        if match is None:
            raise ValueError(
                _message(
                    locale,
                    "Выберите один из перечисленных вариантов",
                    "Choose one of the listed options",
                )
            )
        return match
    elif field.input == "json":
        return json.loads(text)
    else:
        raise ValueError(
            _message(
                locale,
                "Тип поля не поддерживается в чате",
                "This field type is unavailable in chat",
            )
        )
    if field.minimum is not None and value < field.minimum:
        raise ValueError(_message(locale, "Значение ниже минимума", "Value is below the minimum"))
    if field.maximum is not None and value > field.maximum:
        raise ValueError(_message(locale, "Значение выше максимума", "Value is above the maximum"))
    return value


def advance_chat_form(session, pending, text: str, *, actor: str, now: datetime, source: str):
    state = deepcopy(pending.value["chat_form"])
    try:
        form = form_for_action(session, state["action_id"], locale=state["locale"])
        if form.schema_hash != state["schema_hash"]:
            raise Conflict("Form schema changed")
    except (Conflict, LookupError):
        return {
            "response": _message(
                state["locale"],
                "Трекер изменился. Откройте актуальное меню.",
                "Tracker changed. Open the current menu.",
            ),
            "cancelled": True,
        }
    field_order = state["field_order"]
    if len(field_order) != len(form.fields) or set(field_order) != {
        field.name for field in form.fields
    }:
        return {
            "response": _message(
                state["locale"],
                "Трекер изменился. Откройте актуальное меню.",
                "Tracker changed. Open the current menu.",
            ),
            "cancelled": True,
        }
    steps = _steps(form, field_order)
    index = state["step"]
    if index >= len(steps):
        return {
            "response": _message(
                state["locale"],
                "Форма уже заполнена. Откройте трекер заново.",
                "Form already completed. Open the tracker again.",
            ),
            "cancelled": True,
        }
    step = steps[index]
    answer = text.strip()
    editing = state["action_id"].startswith("edit:")
    try:
        if step in {"__start__", "__end__"}:
            current = state["start" if step == "__start__" else "end"]
            if editing and answer == "=":
                value = datetime.fromisoformat(current) if current else None
            elif step == "__end__" and answer.casefold() in {"нет", "none"}:
                value = None
            else:
                value = _time(answer, state["timezone"], now)
            if step == "__end__" and value is None and form.topology == "bounded_interval":
                raise ValueError(
                    _message(state["locale"], "Укажите время окончания", "Enter an end time")
                )
            if (
                step == "__end__"
                and value is not None
                and value < datetime.fromisoformat(state["start"])
            ):
                raise ValueError(
                    _message(
                        state["locale"], "Окончание раньше начала", "End time is before start time"
                    )
                )
            state["start" if step == "__start__" else "end"] = (
                value.isoformat() if value is not None else None
            )
        else:
            field = next(row for row in form.fields if row.name == step.removeprefix("field:"))
            if editing and answer == "=" and field.name in state["values"]:
                value = state["values"][field.name]
            else:
                value = _value(answer, field, state["locale"])
            if value is not None:
                state["values"] = {**state["values"], field.name: value}
                if field.unit:
                    state["units"] = {**state["units"], field.name: field.unit}
            else:
                state["values"].pop(field.name, None)
                state["units"].pop(field.name, None)
    except (ValueError, OverflowError) as exc:
        return {
            "response": f"{exc}. {_prompt(form, index, field_order, locale=state['locale'], state=state)}",
            "written": False,
        }
    index += 1
    state["step"] = index
    pending.value = {**pending.value, "chat_form": state}
    if index < len(steps):
        return {
            "response": _prompt(form, index, field_order, locale=state["locale"], state=state),
            "written": False,
        }
    try:
        submit_form(
            session,
            form.id,
            FormSubmission(
                action_id=form.id,
                schema_hash=state["schema_hash"],
                submission_id=state["submission_id"],
                start=datetime.fromisoformat(state["start"]),
                end=datetime.fromisoformat(state["end"]) if state["end"] else None,
                timezone=state["timezone"],
                values=state["values"],
                units=state["units"],
            ),
            actor=actor,
            source=source,
            idempotency_key=(
                f"telegram-chat:{state['submission_id']}" if state["submission_id"] else None
            ),
        )
    except Conflict:
        return {
            "response": _message(
                state["locale"],
                "Запись изменилась. Откройте /history снова.",
                "Entry changed. Open /history again.",
            ),
            "cancelled": True,
        }
    except FormValidationError as exc:
        state["step"] = len(steps) - len(form.fields)
        state["values"] = dict(form.initial_values) if editing else {}
        state["units"] = dict(form.initial_units) if editing else {}
        pending.value = {**pending.value, "chat_form": state}
        return {
            "response": f"{_message(state['locale'], 'Проверьте значения', 'Check the values')} ({exc.errors}). {_prompt(form, state['step'], field_order, locale=state['locale'], state=state)}",
            "written": False,
        }
    return {
        "response": _message(
            state["locale"],
            "Запись исправлена." if editing else "Запись сохранена.",
            "Entry updated." if editing else "Entry saved.",
        ),
        "written": True,
    }
