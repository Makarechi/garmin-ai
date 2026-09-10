"""Explicit text forms for medication and notes when the model is unavailable."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from garmin_ai.agent import Interpretation, pending_clarification
from garmin_ai.events import EventInput

PROMPTS = {
    "medication": "Напишите: название; доза и единица (mg, mcg, g, ml, tablet, drop, IU); время. Время: сейчас, ЧЧ:ММ или дата и время с UTC-смещением. Доза должна быть указана явно.",
    "note": "Напишите: текст заметки; время. Время: сейчас, ЧЧ:ММ или дата и время с UTC-смещением.",
}


def form_time(value, now, timezone):
    value = value.strip()
    zone = ZoneInfo(timezone)
    if value.casefold() == "сейчас":
        return now.astimezone(zone)
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        hour, minute = map(int, value.split(":"))
        local = now.astimezone(zone)
        wall = local.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)
        if (
            wall.replace(tzinfo=zone, fold=0).utcoffset()
            != wall.replace(tzinfo=zone, fold=1).utcoffset()
        ):
            raise ValueError("DST time requires an explicit offset")
        if wall > local.replace(tzinfo=None):
            wall -= timedelta(days=1)
        candidates = [wall.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
        if candidates[0].utcoffset() != candidates[1].utcoffset():
            raise ValueError("DST time requires an explicit offset")
        result = candidates[0]
    else:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError("Explicit date requires an offset")
        if result.utcoffset() != result.astimezone(zone).utcoffset():
            raise ValueError("Offset must match configured timezone")
    if result.astimezone(UTC) > now.astimezone(UTC) + timedelta(minutes=5):
        raise ValueError("Future fact")
    return result


def interpret_form(session, text, settings, now):
    pending = pending_clarification(session, now)
    button = pending.value.get("button") if pending else None
    if not pending or pending.value.get("action") != "log" or button not in PROMPTS:
        return None
    try:
        if button == "medication":
            name, dose_text, when = (part.strip() for part in text.split(";"))
            matched = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s+(mg|mcg|g|ml|tablet|drop|IU)", dose_text)
            if matched is None:
                raise ValueError("Explicit dose and unit required")
            payload = {
                "type": "medication",
                "name": name,
                "dose": float(matched[1].replace(",", ".")),
                "unit": matched[2],
            }
        else:
            note, when = (part.strip() for part in text.rsplit(";", 1))
            payload = {"type": "note", "description": note}
        event = EventInput(
            start=form_time(when, now, settings.timezone),
            timezone=settings.timezone,
            source="telegram_text",
            original_text=text,
            payload=payload,
        )
        return Interpretation(intent="log", confidence=1, events=[event])
    except (ValueError, OverflowError):
        return Interpretation(
            intent="clarify",
            confidence=0,
            clarification="Не удалось заполнить форму; запись ещё не добавлена. " + PROMPTS[button],
        )
