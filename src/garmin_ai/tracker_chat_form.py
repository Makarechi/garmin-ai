"""Deterministic, durable Telegram entry form for generated trackers."""

import json
import math
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from garmin_ai.events import Conflict
from garmin_ai.tracker_forms import (
    FormSpec,
    FormSubmission,
    FormValidationError,
    form_for_action,
    submit_form,
)


class FormAnswerError(ValueError):
    """A localized validation message safe to show in a chat reply."""


def _message(locale: str, ru: str, en: str) -> str:
    return en if locale.split("-", 1)[0] == "en" else ru


def _steps(form: FormSpec, field_order: list[str] | None = None) -> list[str]:
    names = (
        field_order
        if field_order is not None
        else [row.name for row in form.fields if not row.has_const]
    )
    return [
        "__start__",
        *(["__end__"] if form.topology != "point" else []),
        *[f"field:{name}" for name in names],
    ]


def _choice_labels(options: list) -> list[str]:
    rendered = [str(option) for option in options]
    labels = [
        json.dumps(option, ensure_ascii=False, sort_keys=True)
        if rendered.count(str(option)) > 1
        else str(option)
        for option in options
    ]
    return [f"={label}" if label == "/skip" or label.startswith("=") else label for label in labels]


def _prompt(
    form: FormSpec, index: int, field_order: list[str] | None = None, *, locale: str = "ru"
) -> str:
    step = _steps(form, field_order)[index]
    if step == "__start__":
        return _message(
            locale,
            "Когда началась запись? Ответьте «сейчас» или укажите YYYY-MM-DD HH:MM.",
            "When did the entry start? Reply 'now' or enter YYYY-MM-DD HH:MM.",
        )
    if step == "__end__":
        optional = form.topology != "bounded_interval"
        return _message(
            locale,
            f"Когда запись закончилась? Укажите YYYY-MM-DD HH:MM{' или «нет», если эпизод ещё идёт' if optional else ''}.",
            f"When did the entry end? Enter YYYY-MM-DD HH:MM{" or 'none' if it is still open" if optional else ''}.",
        )
    field = next(row for row in form.fields if row.name == step.removeprefix("field:"))

    def literal(value):
        return re.sub(r"([\\`*_{}\[\]()#+.!<>|~-])", r"\\\1", str(value))

    detail = f" ({literal(field.unit)})" if field.unit else ""
    if field.input == "choice":
        detail += ": " + ", ".join(literal(label) for label in _choice_labels(field.options))
    if field.minimum is not None and field.maximum is not None:
        lower = ">" if field.exclusive_minimum else "≥"
        upper = "<" if field.exclusive_maximum else "≤"
        detail += f" ({lower}{field.minimum:g}, {upper}{field.maximum:g})"
    if field.input == "text" and field.min_length is not None and field.min_length > 1:
        detail += _message(
            locale,
            f" (от {field.min_length} символов)",
            f" ({field.min_length}+ characters)",
        )
    optional = (
        _message(
            locale,
            " Ответьте /skip, чтобы пропустить; =/skip сохранит буквальное значение.",
            " Reply /skip to skip; =/skip saves the literal value.",
        )
        if not field.required
        else ""
    )
    return f"{literal(field.label)}{detail}?{optional}"


def begin_chat_form(pending, form: FormSpec, *, timezone: str, locale: str) -> str:
    """Pin the schema and a stable submission ID before asking the first question."""

    if form.action.kind != "create_entry" or form.submission_id is None:
        raise ValueError("Chat form requires a new tracker entry")
    state = {
        "action_id": form.id,
        "schema_hash": form.schema_hash,
        "submission_id": form.submission_id,
        "timezone": timezone,
        "locale": locale,
        "field_order": [field.name for field in form.fields if not field.has_const],
        "step": 0,
        "start": None,
        "end": None,
        "values": {field.name: field.const_value for field in form.fields if field.has_const},
        "units": {
            field.name: field.unit for field in form.fields if field.has_const and field.unit
        },
    }
    pending.value = {**pending.value, "chat_form": state}
    return _prompt(form, 0, locale=locale)


def _time(text: str, timezone: str, now: datetime, locale: str = "en") -> datetime:
    if text.casefold() in {"сейчас", "now"}:
        return now
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?", text):
        raise FormAnswerError(
            _message(
                locale,
                "Укажите YYYY-MM-DD HH:MM и смещение, если оно требуется",
                "Use YYYY-MM-DD HH:MM, with an offset when required",
            )
        )
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        raise FormAnswerError(
            _message(locale, "Некорректная дата или время", "Invalid calendar time")
        ) from None
    zone = ZoneInfo(timezone)
    if parsed.tzinfo is None:
        first = parsed.replace(tzinfo=zone, fold=0)
        second = parsed.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset() or (
            first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != parsed
        ):
            raise FormAnswerError(
                _message(
                    locale,
                    "Неоднозначное или несуществующее местное время; укажите UTC-смещение",
                    "Ambiguous or nonexistent local time; include a UTC offset",
                )
            )
        parsed = first
    elif parsed.utcoffset() != parsed.astimezone(zone).utcoffset():
        raise FormAnswerError(
            _message(
                locale,
                "UTC-смещение не совпадает с настроенным часовым поясом",
                "UTC offset does not match the configured timezone",
            )
        )
    if parsed.astimezone(UTC) > now.astimezone(UTC) + timedelta(minutes=5):
        raise FormAnswerError(
            _message(locale, "Время не может быть в будущем", "Time cannot be in the future")
        )
    return parsed


def _value(text: str, field, locale: str):
    if field.input in {"text", "choice"} and text.startswith("="):
        text = text[1:]
        literal_answer = True
    else:
        literal_answer = False
    if text == "-" and field.input == "choice" and "-" in field.options:
        return "-"
    if text == "-" and field.input == "text":
        return "-"
    if text == "/skip" and not field.required and not literal_answer:
        return None
    if field.input == "text":
        if (
            not text
            or (field.min_length is not None and len(text) < field.min_length)
            or (field.max_length is not None and len(text) > field.max_length)
        ):
            raise FormAnswerError(
                _message(
                    locale, "Укажите текст допустимой длины", "Enter text within the allowed length"
                )
            )
        return text
    if field.input == "integer":
        if not text.lstrip("-").isdigit():
            raise FormAnswerError(_message(locale, "Нужно целое число", "Enter a whole number"))
        value = int(text)
    elif field.input == "number":
        value = float(text.replace(",", "."))
        if not math.isfinite(value):
            raise FormAnswerError(_message(locale, "Нужно конечное число", "Enter a finite number"))
    elif field.input == "boolean":
        normalized = text.casefold()
        if normalized not in {"да", "нет", "yes", "no", "true", "false"}:
            raise FormAnswerError(_message(locale, "Ответьте «да» или «нет»", "Reply yes or no"))
        return normalized in {"да", "yes", "true"}
    elif field.input == "choice":
        labels = _choice_labels(field.options)
        exact = [
            option
            for option, label in zip(field.options, labels, strict=True)
            if label == (f"={text}" if literal_answer else text)
        ]
        if len(exact) == 1:
            return exact[0]
        folded = [
            option
            for option, label in zip(field.options, labels, strict=True)
            if label.casefold() == (f"={text}" if literal_answer else text).casefold()
        ]
        if len(folded) != 1:
            raise FormAnswerError(
                _message(
                    locale,
                    "Выберите один из перечисленных вариантов",
                    "Choose one of the listed options",
                )
            )
        return folded[0]
    elif field.input == "json":

        def reject_constant(value):
            raise ValueError(f"Non-finite JSON constant: {value}")

        return json.loads(text, parse_constant=reject_constant)
    else:
        raise FormAnswerError(
            _message(
                locale,
                "Тип поля не поддерживается в чате",
                "This field type is unavailable in chat",
            )
        )
    if field.minimum is not None and (
        value < field.minimum or (field.exclusive_minimum and value == field.minimum)
    ):
        raise FormAnswerError(
            _message(
                locale,
                "Значение должно быть выше минимума"
                if field.exclusive_minimum
                else "Значение ниже минимума",
                "Value must exceed the minimum"
                if field.exclusive_minimum
                else "Value is below the minimum",
            )
        )
    if field.maximum is not None and (
        value > field.maximum or (field.exclusive_maximum and value == field.maximum)
    ):
        raise FormAnswerError(
            _message(
                locale,
                "Значение должно быть ниже максимума"
                if field.exclusive_maximum
                else "Значение выше максимума",
                "Value must be below the maximum"
                if field.exclusive_maximum
                else "Value is above the maximum",
            )
        )
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
    if len(field_order) != sum(not field.has_const for field in form.fields) or set(
        field_order
    ) != {field.name for field in form.fields if not field.has_const}:
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
    try:
        if step in {"__start__", "__end__"}:
            value = (
                None
                if step == "__end__" and answer.casefold() in {"нет", "none"}
                else _time(answer, state["timezone"], now, state["locale"])
            )
            if step == "__end__" and value is None and form.topology == "bounded_interval":
                raise FormAnswerError(
                    _message(state["locale"], "Укажите время окончания", "Enter an end time")
                )
            if (
                step == "__end__"
                and value is not None
                and (
                    value <= datetime.fromisoformat(state["start"])
                    if form.topology == "bounded_interval"
                    else value < datetime.fromisoformat(state["start"])
                )
            ):
                raise FormAnswerError(
                    _message(
                        state["locale"], "Окончание раньше начала", "End time is before start time"
                    )
                )
            state["start" if step == "__start__" else "end"] = (
                value.isoformat() if value is not None else None
            )
        else:
            field = next(row for row in form.fields if row.name == step.removeprefix("field:"))
            field_answer = text if field.input in {"text", "choice"} else answer
            value = _value(field_answer, field, state["locale"])
            if value is not None or (field.input in {"choice", "json"} and answer != "/skip"):
                state["values"] = {**state["values"], field.name: value}
                if field.unit:
                    state["units"] = {**state["units"], field.name: field.unit}
    except FormAnswerError as exc:
        pending.value = {**pending.value, "created_at": now.isoformat()}
        return {
            "response": f"{exc}. {_prompt(form, index, field_order, locale=state['locale'])}",
            "written": False,
        }
    except (ValueError, OverflowError):
        pending.value = {**pending.value, "created_at": now.isoformat()}
        return {
            "response": _message(
                state["locale"],
                "Не удалось разобрать ответ. ",
                "Could not parse that answer. ",
            )
            + _prompt(form, index, field_order, locale=state["locale"]),
            "written": False,
        }
    index += 1
    state["step"] = index
    pending.value = {**pending.value, "chat_form": state, "created_at": now.isoformat()}
    if index < len(steps):
        return {
            "response": _prompt(form, index, field_order, locale=state["locale"]),
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
            idempotency_key=f"telegram-chat:{state['submission_id']}",
        )
    except (Conflict, LookupError):
        return {
            "response": _message(
                state["locale"],
                "Трекер изменился. Откройте актуальное меню.",
                "Tracker changed. Open the current menu.",
            ),
            "cancelled": True,
        }
    except FormValidationError:
        state["step"] = len(steps) - len(field_order)
        state["values"] = {
            field.name: field.const_value for field in form.fields if field.has_const
        }
        state["units"] = {
            field.name: field.unit for field in form.fields if field.has_const and field.unit
        }
        pending.value = {**pending.value, "chat_form": state, "created_at": now.isoformat()}
        return {
            "response": f"{_message(state['locale'], 'Проверьте значения', 'Check the values')}. {_prompt(form, state['step'], field_order, locale=state['locale'])}",
            "written": False,
        }
    return {
        "response": _message(state["locale"], "Запись сохранена.", "Entry saved."),
        "written": True,
    }
