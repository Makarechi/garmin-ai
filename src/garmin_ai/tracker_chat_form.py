"""Deterministic, durable Telegram entry form for generated trackers."""

import json
import math
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from garmin_ai.events import Conflict
from garmin_ai.i18n import normalized_locale
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
    return ru if normalized_locale(locale) == "ru" else en


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


def _display_time(value: str | None, timezone: str) -> str:
    if value is None:
        return "—"
    return (
        datetime.fromisoformat(value)
        .astimezone(ZoneInfo(timezone))
        .isoformat(sep=" ", timespec="minutes")
    )


def _choice_labels(options: list) -> list[str]:
    labels = ["/empty" if option == "" else str(option) for option in options]
    escaped = [f"={label}" if label.startswith(("/", "=")) else label for label in labels]
    if len(set(escaped)) == len(escaped):
        return escaped
    return [
        f"{index + 1}: {json.dumps(option, ensure_ascii=False, sort_keys=True)}"
        for index, option in enumerate(options)
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
        return prompt + (
            f" {_display_time(state['start'], state['timezone'])}.{keep}" if editing else ""
        )
    if step == "__end__":
        optional = form.topology != "bounded_interval"
        prompt = _message(
            locale,
            f"Когда запись закончилась? Укажите YYYY-MM-DD HH:MM{' или «нет», если эпизод ещё идёт' if optional else ''}.",
            f"When did the entry end? Enter YYYY-MM-DD HH:MM{" or 'none' if it is still open" if optional else ''}.",
        )
        return prompt + (
            f" {_display_time(state['end'], state['timezone'])}.{keep}" if editing else ""
        )
    field = next(row for row in form.fields if row.name == step.removeprefix("field:"))

    def literal(value):
        return re.sub(r"([\\`*_{}\[\]()#+.!<>|~-])", r"\\\1", str(value))

    detail = f" ({literal(field.unit)})" if field.unit else ""
    if field.input == "choice":
        detail += ": " + ", ".join(literal(label) for label in _choice_labels(field.options))
    bounds = []
    if field.minimum is not None:
        bounds.append(f"{'> ' if field.exclusive_minimum else '≥ '}{field.minimum}")
    if field.maximum is not None:
        bounds.append(f"{'< ' if field.exclusive_maximum else '≤ '}{field.maximum}")
    if bounds:
        detail += " (" + ", ".join(bounds) + ")"
    if field.input == "text" and field.min_length is not None and field.min_length > 1:
        detail += _message(
            locale,
            f" (от {field.min_length} символов)",
            f" ({field.min_length}+ characters)",
        )
    if field.input == "text":
        detail += _message(
            locale,
            " (для пустого значения ответьте =/empty; для буквальной команды начните с =)",
            " (reply =/empty for an empty value; prefix = to enter a command literally)",
        )
        if field.max_length is not None:
            detail += _message(
                locale,
                f" (до {field.max_length} символов)",
                f" (up to {field.max_length} characters)",
            )
    if field.input == "json":
        detail += _message(
            locale,
            ' (отправьте JSON, например ["a", "b"] или {"key": "value"})',
            ' (send JSON, for example ["a", "b"] or {"key": "value"})',
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
    current = state["values"].get(field.name) if editing else None
    literal_equals = _message(
        locale,
        " Для значения «=» ответьте «==».",
        " Reply '==' to enter a literal '='.",
    )
    return f"{literal(field.label)}{detail}?{optional}" + (
        f" {literal(current)}.{keep}{literal_equals if field.input in {'text', 'choice'} else ''}"
        if current is not None
        else ""
    )


def begin_chat_form(pending, form: FormSpec, *, timezone: str, locale: str) -> str:
    """Pin the schema, revision and submission identity before the first answer."""

    if form.action.kind not in {"create_entry", "edit_entry"}:
        raise ValueError("Chat form requires a tracker entry action")
    if form.action.kind == "create_entry" and form.submission_id is None:
        raise ValueError("Create form requires a submission ID")
    if form.conditional_requirements:
        raise FormAnswerError(
            _message(
                locale,
                "У этого трекера условные обязательные поля. Заполните его в приложении.",
                "This tracker has conditional required fields. Fill it in the app.",
            )
        )
    if any(
        field.required
        and (form.action.kind == "create_entry" or field.name not in form.initial_values)
        and (
            (field.input == "text" and (field.min_length or 0) > 4096)
            or (
                field.input == "choice"
                and all(len(label) > 4096 for label in _choice_labels(field.options))
            )
            or (
                field.input == "json"
                and ((field.min_json_length or 0) > 4096 or field.complex_json)
            )
        )
        for field in form.fields
        if not field.has_const
    ):
        raise FormAnswerError(
            _message(
                locale,
                "Поле требует ответ длиннее лимита Telegram. Заполните трекер в приложении.",
                "A field requires an answer longer than Telegram allows. Use the app to fill this tracker.",
            )
        )
    state = {
        "action_id": form.id,
        "schema_hash": form.schema_hash,
        "submission_id": form.submission_id,
        "timezone": form.initial_timezone or timezone,
        "locale": locale,
        "field_order": [field.name for field in form.fields if not field.has_const],
        "step": 0,
        "start": form.initial_start.isoformat() if form.initial_start else None,
        "end": form.initial_end.isoformat() if form.initial_end else None,
        "event_topology": form.initial_topology,
        "end_kept": False,
        "values": {
            **form.initial_values,
            **{
                field.name: field.const_value
                for field in form.fields
                if field.has_const
                and (form.action.kind == "create_entry" or field.name in form.initial_values)
            },
        },
        "units": {
            **form.initial_units,
            **{
                field.name: field.unit
                for field in form.fields
                if field.has_const
                and field.unit
                and (form.action.kind == "create_entry" or field.name in form.initial_values)
            },
        },
    }
    pending.value = {
        **pending.value,
        "chat_form": state,
        "created_at": datetime.now(UTC).isoformat(),
    }
    return _prompt(form, 0, locale=locale, state=state)


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


def begin_close_chat_form(pending, form: FormSpec, *, locale: str) -> str:
    if (
        form.action.kind != "edit_entry"
        or form.topology != "open_interval"
        or form.initial_end is not None
    ):
        raise ValueError("Close form requires an open tracker entry")
    pending.value = {
        **pending.value,
        "chat_close": {
            "action_id": form.id,
            "schema_hash": form.schema_hash,
            "locale": locale,
        },
    }
    return _message(
        locale,
        "Когда завершилась запись? Ответьте «сейчас» или укажите YYYY-MM-DD HH:MM.",
        "When did the entry end? Reply 'now' or enter YYYY-MM-DD HH:MM.",
    )


def advance_close_chat_form(session, pending, text: str, *, actor: str, now: datetime, source: str):
    state = pending.value["chat_close"]
    locale = state["locale"]
    refresh_at = session.info.get("conversation_now", now)
    try:
        form = form_for_action(session, state["action_id"], locale=locale)
        if form.schema_hash != state["schema_hash"] or form.initial_end is not None:
            raise Conflict("Close form changed")
    except (Conflict, LookupError):
        return {
            "response": _message(
                locale,
                "Запись изменилась. Откройте /history снова.",
                "Entry changed. Open /history again.",
            ),
            "cancelled": True,
        }
    try:
        end = _time(text.strip(), form.initial_timezone, now, locale)
        if end <= form.initial_start:
            raise FormAnswerError(
                _message(locale, "Окончание должно быть позже начала", "End must be after start")
            )
    except (FormAnswerError, ValueError, OverflowError) as exc:
        detail = (
            str(exc)
            if isinstance(exc, FormAnswerError)
            else _message(locale, "Некорректное время", "Invalid time")
        )
        pending.value = {**pending.value, "created_at": refresh_at.isoformat()}
        return {
            "response": f"{detail}. {begin_close_chat_form(pending, form, locale=locale)}",
            "written": False,
        }
    try:
        submit_form(
            session,
            form.id,
            FormSubmission(
                action_id=form.id,
                schema_hash=form.schema_hash,
                start=form.initial_start,
                end=end,
                timezone=form.initial_timezone,
                values=form.initial_values,
                units=form.initial_units,
            ),
            actor=actor,
            source=source,
        )
    except (Conflict, LookupError):
        return {
            "response": _message(
                locale,
                "Запись изменилась. Откройте /history снова.",
                "Entry changed. Open /history again.",
            ),
            "cancelled": True,
        }
    except FormValidationError:
        return {
            "response": _message(
                locale,
                "Значения записи требуют исправления. Откройте /history и выберите «Исправить».",
                "Entry values need correction. Open /history and choose Edit.",
            ),
            "cancelled": True,
        }
    return {
        "response": _message(locale, "Запись завершена.", "Entry closed."),
        "written": True,
    }


def _value(text: str, field, locale: str):
    if field.input in {"text", "choice"} and text == "=/empty":
        text = ""
        literal_answer = True
    elif field.input in {"text", "choice"} and text.startswith("="):
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
        if text.startswith("/") and not literal_answer:
            raise FormAnswerError(
                _message(locale, "Начните буквальное значение с =", "Prefix a literal value with =")
            )
        if (field.min_length is not None and len(text) < field.min_length) or (
            field.max_length is not None and len(text) > field.max_length
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
        if "," in text and normalized_locale(locale) == "en":
            raise FormAnswerError(_message(locale, "Укажите число с точкой", "Use a decimal point"))
        try:
            exact = Decimal(text.replace(",", "."))
        except InvalidOperation:
            raise FormAnswerError(_message(locale, "Нужно число", "Enter a number")) from None
        if not exact.is_finite():
            raise FormAnswerError(_message(locale, "Нужно конечное число", "Enter a finite number"))
        if exact == exact.to_integral_value():
            if exact and exact.adjusted() >= 4096:
                raise FormAnswerError(
                    _message(locale, "Число слишком длинное", "Number is too long")
                )
            value = int(exact)
        else:
            value = float(exact)
            if not math.isfinite(value) or Decimal(str(value)) != exact:
                raise FormAnswerError(
                    _message(
                        locale,
                        "Слишком много знаков для точной записи",
                        "Too many digits to save exactly",
                    )
                )
    elif field.input == "boolean":
        normalized = text.casefold()
        if normalized not in {"да", "нет", "yes", "no", "true", "false"}:
            raise FormAnswerError(_message(locale, "Ответьте «да» или «нет»", "Reply yes or no"))
        return normalized in {"да", "yes", "true"}
    elif field.input == "choice":
        labels = _choice_labels(field.options)
        target = (
            "=/empty" if literal_answer and text == "" else f"={text}" if literal_answer else text
        )
        exact = [
            option for option, label in zip(field.options, labels, strict=True) if label == target
        ]
        if len(exact) == 1:
            return exact[0]
        folded = [
            option
            for option, label in zip(field.options, labels, strict=True)
            if label.casefold() == target.casefold()
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

        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON field")
                result[key] = value
            return result

        value = json.loads(
            text, parse_constant=reject_constant, object_pairs_hook=reject_duplicate_keys
        )

        def finite_json(item):
            if isinstance(item, float):
                return math.isfinite(item)
            if isinstance(item, list):
                return all(finite_json(child) for child in item)
            if isinstance(item, dict):
                return all(finite_json(child) for child in item.values())
            return True

        if not finite_json(value):
            raise ValueError("Non-finite JSON number")
        return value
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


def advance_chat_form(
    session,
    pending,
    text: str,
    *,
    actor: str,
    now: datetime,
    source: str,
    processed_at: datetime | None = None,
):
    refresh_at = processed_at or session.info.get("conversation_now", now)
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
    editing = state["action_id"].startswith("edit:")
    try:
        if step in {"__start__", "__end__"}:
            current = state["start" if step == "__start__" else "end"]
            if editing and answer == "=":
                value = datetime.fromisoformat(current) if current else None
                if step == "__end__":
                    state["end_kept"] = True
            elif step == "__end__" and answer.casefold() in {"нет", "none"}:
                value = None
                state["end_kept"] = False
            else:
                value = _time(answer, state["timezone"], now, state["locale"])
                if step == "__end__":
                    state["end_kept"] = False
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
            if editing and answer == "=" and field.name in state["values"]:
                value = state["values"][field.name]
            else:
                field_answer = text if field.input in {"text", "choice"} else answer
                value = _value(field_answer, field, state["locale"])
            if value is not None or (field.input in {"choice", "json"} and answer != "/skip"):
                state["values"] = {**state["values"], field.name: value}
                if field.unit:
                    state["units"] = {**state["units"], field.name: field.unit}
            else:
                state["values"].pop(field.name, None)
                state["units"].pop(field.name, None)
    except FormAnswerError as exc:
        pending.value = {**pending.value, "created_at": refresh_at.isoformat()}
        return {
            "response": f"{exc}. {_prompt(form, index, field_order, locale=state['locale'], state=state)}",
            "written": False,
        }
    except (ValueError, OverflowError, RecursionError):
        pending.value = {**pending.value, "created_at": refresh_at.isoformat()}
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
    pending.value = {**pending.value, "chat_form": state, "created_at": refresh_at.isoformat()}
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
                end=(
                    datetime.fromisoformat(state["end"])
                    if state["end"]
                    else datetime.fromisoformat(state["start"])
                    if editing and state.get("event_topology") == "point" and state.get("end_kept")
                    else None
                ),
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
                "Запись изменилась. Откройте /history снова."
                if editing
                else "Трекер изменился. Откройте актуальное меню.",
                "Entry changed. Open /history again."
                if editing
                else "Tracker changed. Open the current menu.",
            ),
            "cancelled": True,
        }
    except LookupError:
        return {
            "response": _message(
                state["locale"],
                "Трекер изменился. Откройте актуальное меню.",
                "Tracker changed. Open the current menu.",
            ),
            "cancelled": True,
        }
    except ValueError as exc:
        if not isinstance(exc, FormValidationError) and "too large" not in str(exc):
            raise
        if not field_order:
            return {
                "response": _message(
                    state["locale"],
                    "Схема трекера не позволяет заполнить запись. Исправьте определение трекера.",
                    "This tracker schema cannot produce a valid entry. Correct its definition.",
                ),
                "cancelled": True,
            }
        state["step"] = len(steps) - len(field_order)
        state["values"] = {
            **(form.initial_values if editing else {}),
            **{
                field.name: field.const_value
                for field in form.fields
                if field.has_const and (not editing or field.name in form.initial_values)
            },
        }
        state["units"] = {
            **(form.initial_units if editing else {}),
            **{
                field.name: field.unit
                for field in form.fields
                if field.has_const
                and field.unit
                and (not editing or field.name in form.initial_values)
            },
        }
        pending.value = {**pending.value, "chat_form": state, "created_at": refresh_at.isoformat()}
        return {
            "response": f"{_message(state['locale'], 'Проверьте значения' if isinstance(exc, FormValidationError) else 'Сократите значения', 'Check the values' if isinstance(exc, FormValidationError) else 'Shorten the values')}. {_prompt(form, state['step'], field_order, locale=state['locale'], state=state)}",
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
