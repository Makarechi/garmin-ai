"""Conservative literal evidence for newly reported incomplete medication intakes."""

import re
from datetime import datetime, timedelta

VERB = r"\b(?:принял[аи]?|выпил[аи]?|принимал[аи]?|took|taken)\b"
QUESTION = r"[?]|\b(?:если|бы|например|допустим|цитата|кажется|возможно|наверное|вероятно|обычно|всегда|ежедневно|каждый|каждое|каждую|if|would|suppose|example|maybe|perhaps|probably|think|usually|always|daily|every)\b"
NEGATIVE = (
    r"\b(?:не|ничего|нет|not|never|ли|(?:did|have|has|had|was|were|is|are|do|does)n['’]t)\b"
    r"|^\s*(?:did|have|has|had|was|were|is|are|do|does|when|why|what|how)\b"
)
NUMBERS = {
    "один": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "one": 1,
    "two": 2,
    "three": 3,
}
QUANTITY = r"(?:\d+|один|одну|два|две|три|четыре|пять|one|two|three)"
UNIT = r"(?:час(?:а|ов)?|минут(?:у|ы)?|hours?|minutes?)"
CLOCK = r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})|\bсейчас\b|\bnow\b|\b\d{1,2}:\d{2}\b|\bв\s+\d{1,2}\b"
RELATIVE = rf"\b(?:(?P<n>{QUANTITY})\s+(?P<u>{UNIT})|(?P<u2>{UNIT})\s+(?P<n2>{QUANTITY}))\s+(?:назад|ago)\b"
OTHER_SUBJECT = r"\b(?:он|она|они|муж|жена|мама|папа|сын|дочь|реб[её]нок|брат|сестра|he|she|they|husband|wife|mother|father|son|daughter)\b"
DOSE = r"\b\d+(?:[.,]\d+)?\s*(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблетк[ауи]?|tablets?|кап(?:ля|ли|ель)|drops?)\b"
GENERIC = r"\b(?:таблетк[ауи]|лекарство|medicine|tablets?|pill|я|i|сегодня|вчера|утром|вечером|уже|снова|today|yesterday|just|have)\b"


def owner_assertion(clause):
    verb = re.search(VERB, clause, re.I)
    if verb is None or re.search(NEGATIVE + "|" + OTHER_SUBJECT, clause, re.I):
        return False
    # An unspecified pre-verbal subject is not evidence about the owner.
    prefix = re.sub(RELATIVE, "", clause[: verb.start()], flags=re.I)
    prefix = re.sub(CLOCK, "", prefix, flags=re.I)
    prefix = re.sub(rf"\bчерез\s+{QUANTITY}\s+{UNIT}\b", "", prefix, flags=re.I)
    prefix = re.sub(GENERIC, "", prefix, flags=re.I)
    return not prefix.strip(" ,;:")


def medication_phrase(sentence):
    verb = re.search(VERB, sentence, re.I)
    if verb is None:
        return ""
    return re.split(
        r"[,;]|\b(?:после|до|запил[аи]?|after|before|with)\b", sentence[verb.end() :], flags=re.I
    )[0]


def named_object_order(text, events):
    for event in events:
        name = getattr(event.payload, "name", None)
        if not name:
            continue
        pattern = (
            rf"\b({re.escape(name)})\s+({VERB})(?!\s+(?:таблетк|лекарств|medicine|pill|tablet))"
        )
        text = re.sub(pattern, lambda match: match[2] + " " + match[1], text, flags=re.I)
    return text


def literal_names(sentence):
    tail = medication_phrase(sentence)
    tail = re.sub(RELATIVE, "", tail, flags=re.I)
    tail = re.sub(CLOCK, "", tail, flags=re.I)
    tail = re.sub(DOSE, "", tail, flags=re.I)
    tail = re.sub(GENERIC, "", tail, flags=re.I)
    return {
        part.strip().casefold()
        for part in re.split(r"\b(?:и|and)\b", tail, flags=re.I)
        if re.fullmatch(r"[\w-]+(?:\s+[\w-]+)*", part.strip())
    }


def distinct_named_intakes(events, text, now, timezone):
    names = [event.payload.name.casefold() if event.payload.name else None for event in events]
    if None in names or len(set(names)) != len(names):
        return False
    for sentence in re.split(r"(?<=[!?])|[;\n]|\.(?!\d)", unquote_names(text)):
        if events[0].start in reported_intake_times(sentence, now, timezone):
            if set(names) <= literal_names(sentence):
                return True
    return False


def unquote_names(text):
    # A quoted verb is not an assertion by the sender; quoted names are fine.
    return re.sub(
        r'«[^»]*»|"[^"]*"', lambda m: "" if re.search(VERB, m[0], re.I) else m[0][1:-1], text
    )


def explicit_times(text, now, timezone):
    from garmin_ai.diary_forms import form_time

    times = set()
    for match in re.finditer(RELATIVE, text, re.I):
        delta = duration(match["n"] or match["n2"], match["u"] or match["u2"])
        if delta is not None:
            times.add(now - delta)
    for match in re.finditer(CLOCK, text, re.I):
        value = match[0]
        if value.casefold() == "now":
            value = "сейчас"
        if re.fullmatch(r"в\s+\d{1,2}", value, re.I):
            if re.match(
                r"\s+(?:при[её]м|раз|этап|доз|таблет|кап|мг|мл|mg|ml)", text[match.end() :], re.I
            ):
                continue
            value = value.split()[-1] + ":00"
        try:
            times.add(form_time(value, now, timezone))
        except (ValueError, OverflowError):
            pass
    return times


def duration(number, unit):
    raw = number.casefold()
    count = int(raw) if raw.isdigit() else NUMBERS[raw]
    if count > 525600:
        return None
    return timedelta(minutes=count * (60 if unit.casefold().startswith(("час", "hour")) else 1))


def reported_intake_times(text, now, timezone):
    text = unquote_names(text)
    times = set()
    relative = rf"\b(?:(?P<n>{QUANTITY})\s+(?P<u>{UNIT})|(?P<u2>{UNIT})\s+(?P<n2>{QUANTITY}))\s+(?:назад|ago)\b"
    # Keep comma-separated unknown-detail qualifiers attached to an intake,
    # but never borrow the clock of a separate symptom or activity assertion.
    for sentence in re.split(r"(?<=[!?])|[;\n]|\.(?!\d)", text):
        if re.search(QUESTION, sentence, re.I):
            continue
        clauses = re.split(r"[,;]|\b(?:но|but)\b", sentence, flags=re.I)
        anchor = set()
        active = False
        for clause in clauses:
            if re.search(VERB, clause, re.I):
                active = owner_assertion(clause)
                if not active:
                    continue
                times.update(explicit_times(clause, now, timezone))
                for match in re.finditer(relative, clause, re.I):
                    delta = duration(match["n"] or match["n2"], match["u"] or match["u2"])
                    if delta is not None:
                        times.add(now - delta)
                for match in re.finditer(rf"\bчерез\s+({QUANTITY})\s+({UNIT})\b", clause, re.I):
                    delta = duration(match[1], match[2])
                    if delta is not None and len(anchor) == 1:
                        times.add(next(iter(anchor)) + delta)
            elif re.search(
                r"\b(?:мигрень|мигрени|головная боль)\b.*\b(?:начал|нача)", clause, re.I
            ):
                anchor = explicit_times(clause, now, timezone)
                active = False
            elif active:
                # A bare clock or an unknown name/dose continues the medication clause.
                remainder = re.sub(RELATIVE, "", clause, flags=re.I)
                remainder = re.sub(CLOCK, "", remainder, flags=re.I).strip()
                if not remainder or re.fullmatch(
                    r"(?:название|дозу|доза|имя|name|dose)\s+(?:не (?:помню|знаю)|неизвестн[ао]|unknown)",
                    remainder,
                    re.I,
                ):
                    times.update(explicit_times(clause, now, timezone))
                else:
                    active = False
    return times


def clarified_intake_times(text, now, timezone, pending):
    times = reported_intake_times(text, now, timezone)
    if not pending or re.search(VERB + "|" + QUESTION, text, re.I):
        return times
    # Only a detail reply may complete an earlier assertion.
    remainder = re.sub(RELATIVE, "", text, flags=re.I)
    remainder = re.sub(CLOCK, "", remainder, flags=re.I).strip(" ,;")
    if remainder and not re.fullmatch(
        r"(?:название|дозу|доза|имя|name|dose)\s+(?:не (?:помню|знаю)|неизвестн[ао]|unknown)",
        remainder,
        re.I,
    ):
        return times
    messages = pending.get("messages") or [
        {"text": pending.get("text", ""), "at": pending.get("created_at")}
    ]
    for message in messages:
        try:
            stamp = datetime.fromisoformat(message["at"])
            if stamp.utcoffset() is None:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        previous = message.get("text", "")
        original = reported_intake_times(previous, stamp, timezone)
        times.update(original)
        if not original:
            times.update(reported_intake_times(previous + ", " + text, now, timezone))
    return times


def missing_reported_details(event, text, now, timezone, pending):
    """Reject an incomplete extraction that discards a literal dose or named dose."""
    messages = [(text, now)]
    for message in (pending or {}).get("messages", []):
        try:
            stamp = datetime.fromisoformat(message["at"])
            if stamp.utcoffset() is not None:
                messages.append((message.get("text", ""), stamp))
        except (KeyError, ValueError, TypeError):
            continue
    for message, stamp in messages:
        message = named_object_order(message, [event])
        for sentence in re.split(r"(?<=[!?])|[;\n]|\.(?!\d)", unquote_names(message)):
            if event.start not in reported_intake_times(sentence, stamp, timezone):
                continue
            names = literal_names(sentence)
            if names and (event.payload.name is None or event.payload.name.casefold() not in names):
                return True
            if re.search(DOSE, medication_phrase(sentence), re.I):
                if event.payload.dose is None or event.payload.unit is None:
                    return True
                named_dose = re.search(rf"{VERB}\s+([\w-]+)\s+{DOSE}", sentence, re.I)
                if (
                    named_dose
                    and named_dose[1].casefold()
                    not in {"таблетку", "таблетки", "лекарство", "medicine", "tablet", "tablets"}
                    and event.payload.name is None
                ):
                    return True
    return False
