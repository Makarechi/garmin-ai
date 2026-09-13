"""Conservative literal evidence for newly reported incomplete medication intakes."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
    "an": 1,
    "a": 1,
    "two": 2,
    "three": 3,
}
QUANTITY = r"(?:\d+|один|одну|два|две|три|четыре|пять|one|two|three|an|a)"
UNIT = r"(?:час(?:а|ов)?|минут(?:у|ы)?|hours?|minutes?)"
CLOCK = r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})|\bсейчас\b|\bnow\b|\b\d{1,2}:\d{2}\b|\bв\s+\d{1,2}\b"
RELATIVE = rf"\b(?:(?:(?P<n>{QUANTITY})\s+)?(?P<u>{UNIT})|(?P<u2>{UNIT})\s+(?P<n2>{QUANTITY}))\s+(?:назад|ago)\b"
OTHER_SUBJECT = r"\b(?:он|она|они|муж|жена|мама|папа|сын|дочь|реб[её]нок|брат|сестра|he|she|they|husband|wife|mother|father|son|daughter)\b"
DOSE = r"\b\d+(?:[.,]\d+)?\s*(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблетк[ауи]?|tablets?|кап(?:ля|ли|ель)|drops?)\b"
GENERIC = r"\b(?:таблетк[ауи]|лекарство|medicine|tablets?|pill|я|i|сегодня|вчера|утром|вечером|утра|вечера|дня|ночи|уже|снова|today|yesterday|just|have)\b"
UNKNOWN = r"\b(?:неизвестн\w*|какую-то|какой-то|какие-то|unknown|some)\b"
UNKNOWN_DETAIL = r"\b(?:и\s+)?не\s+(?:помню|знаю)\b"


def owner_assertion(clause):
    verb = re.search(VERB, clause, re.I)
    predicate = re.split(UNKNOWN_DETAIL, clause, flags=re.I)[0]
    if verb is None or re.search(NEGATIVE + "|" + OTHER_SUBJECT, predicate, re.I):
        return False
    if (
        re.fullmatch(r"выпил[аи]?", verb[0], re.I)
        and not re.search(r"\b(?:таблетк\w*|лекарств\w*|medicine|pills?|tablets?)\b", clause, re.I)
        and not literal_names(clause)
    ):
        return False
    # An unspecified pre-verbal subject is not evidence about the owner.
    prefix = re.sub(RELATIVE, "", clause[: verb.start()], flags=re.I)
    prefix = re.sub(CLOCK, "", prefix, flags=re.I)
    prefix = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", prefix)
    prefix = re.sub(rf"\bчерез\s+{QUANTITY}\s+{UNIT}\b", "", prefix, flags=re.I)
    prefix = re.sub(GENERIC, "", prefix, flags=re.I)
    return not prefix.strip(" ,;:")


def medication_phrase(sentence):
    verb = re.search(VERB, sentence, re.I)
    if verb is None:
        return ""
    return re.split(
        rf"[,;]|{UNKNOWN_DETAIL}|\b(?:после|до|запил[аи]?|after|before|with)\b",
        sentence[verb.end() :],
        flags=re.I,
    )[0]


def named_object_order(text, events):
    for event in events:
        name = getattr(event.payload, "name", None)
        if not name:
            continue
        pattern = (
            rf"\b({re.escape(name)})\s+({VERB})(?!\s+(?:таблетк|лекарств|medicine|pill|tablet))"
        )

        def reorder(match, original=text):
            tail = re.split(r"[,;.!?]", original[match.end() :], maxsplit=1)[0]
            if re.search(r"\b(?:таблетк\w*|лекарств\w*|medicine|pills?|tablets?)\b", tail, re.I):
                return match[0]
            if re.search(OTHER_SUBJECT, match[1], re.I):
                return match[0]
            return match[2] + " " + match[1]

        text = re.sub(pattern, reorder, text, flags=re.I)
    return text


def literal_names(sentence):
    tail = medication_phrase(sentence)
    tail = re.sub(RELATIVE, "", tail, flags=re.I)
    tail = re.sub(CLOCK, "", tail, flags=re.I)
    tail = re.sub(DOSE, "", tail, flags=re.I)
    tail = re.sub(GENERIC, "", tail, flags=re.I)
    tail = re.sub(r"\bот\s+[\w-]+", "", tail, flags=re.I)
    tail = re.sub(UNKNOWN, "", tail, flags=re.I)
    tail = re.sub(rf"\b{QUANTITY}\b", "", tail, flags=re.I)
    return {
        part.strip().casefold()
        for part in re.split(r"\b(?:и|and)\b", tail, flags=re.I)
        if re.fullmatch(r"[\w-]+(?:\s+[\w-]+)*", part.strip())
    }


def distinct_named_intakes(events, text, now, timezone):
    names = [event.payload.name.casefold() if event.payload.name else None for event in events]
    if None in names or len(set(names)) != len(names):
        return False
    matched = set()
    for sentence in intake_sentences(unquote_names(text)):
        if events[0].start in reported_intake_times(sentence, now, timezone):
            matched.update(literal_names(sentence))
    return set(names) <= matched


def intake_sentences(text):
    for sentence in re.split(r"(?<=[!?])|[;\n]|\.(?!\d)", text):
        parts = re.split(r"\b(?:и|and)\b", sentence, flags=re.I)
        if (
            len(parts) > 1
            and owner_assertion(parts[0])
            and all(re.search(CLOCK, part, re.I) for part in parts)
        ):
            for part in parts:
                yield part if re.search(VERB, part, re.I) else "принял " + part
        else:
            yield sentence


def unquote_names(text):
    # A quoted verb is not an assertion by the sender; quoted names are fine.
    return re.sub(
        r'«[^»]*»|"[^"]*"', lambda m: "" if re.search(VERB, m[0], re.I) else m[0][1:-1], text
    )


def explicit_times(text, now, timezone):
    from garmin_ai.diary_forms import form_time

    times = set()
    if re.search(r"\b(?:или|либо|or)\b", text, re.I):
        return times
    for match in re.finditer(RELATIVE, text, re.I):
        delta = duration(match["n"] or match["n2"] or "1", match["u"] or match["u2"])
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
        daypart = re.match(r"\s+(утра|вечера|дня|ночи)\b", text[match.end() :], re.I)
        if daypart and re.fullmatch(r"\d{1,2}:\d{2}", value):
            hour, minute = map(int, value.split(":"))
            if daypart[1].casefold() in {"вечера", "дня"} and 1 <= hour < 12:
                hour += 12
            elif daypart[1].casefold() in {"утра", "ночи"} and hour == 12:
                hour = 0
            value = f"{hour:02}:{minute:02}"
        try:
            if re.fullmatch(r"\d{1,2}:\d{2}", value) and re.search(
                r"\b(?:вчера|сегодня|yesterday|today)\b|\b\d{4}-\d{2}-\d{2}\b", text, re.I
            ):
                local = now.astimezone(ZoneInfo(timezone))
                day = local.date()
                explicit_date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
                if explicit_date:
                    day = datetime.fromisoformat(explicit_date[0]).date()
                elif re.search(r"\b(?:вчера|yesterday)\b", text, re.I):
                    day -= timedelta(days=1)
                hour, minute = map(int, value.split(":"))
                wall = datetime(
                    day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(timezone)
                )
                if wall.utcoffset() != wall.replace(fold=1).utcoffset():
                    continue
                times.add(form_time(wall.isoformat(), now, timezone))
            else:
                times.add(form_time(value, now, timezone))
        except (ValueError, OverflowError):
            pass
    return times


def duration(number, unit):
    raw = number.casefold()
    if raw.isdigit() and len(raw) > 6:
        return None
    count = int(raw) if raw.isdigit() else NUMBERS[raw]
    if count > 525600:
        return None
    return timedelta(minutes=count * (60 if unit.casefold().startswith(("час", "hour")) else 1))


def reported_intake_times(text, now, timezone):
    text = unquote_names(text)
    times = set()
    relative = RELATIVE
    # Keep comma-separated unknown-detail qualifiers attached to an intake,
    # but never borrow the clock of a separate symptom or activity assertion.
    for sentence in intake_sentences(text):
        if re.search(QUESTION + r"|\b(?:или|либо|or)\b", sentence, re.I):
            continue
        clauses = re.split(r"[,;]|\b(?:но|but)\b", sentence, flags=re.I)
        anchor = set()
        active = False
        for clause in clauses:
            if re.search(VERB, clause, re.I):
                active = owner_assertion(clause)
                if not active:
                    continue
                verb = re.search(VERB, clause, re.I)
                clause = clause[: verb.end()] + medication_phrase(clause)
                times.update(explicit_times(clause, now, timezone))
                for match in re.finditer(relative, clause, re.I):
                    delta = duration(match["n"] or match["n2"] or "1", match["u"] or match["u2"])
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
    return set().union(
        *(
            reported_intake_times(message, stamp, timezone)
            for message, stamp in assertion_messages(text, now, timezone, pending)
        )
    )


def assertion_messages(text, now, timezone, pending):
    messages_with_time = [(text, now)]
    if not pending or re.search(QUESTION, text, re.I):
        return messages_with_time
    # Only a detail reply may complete an earlier assertion.
    remainder = re.sub(RELATIVE, "", text, flags=re.I)
    remainder = re.sub(CLOCK, "", remainder, flags=re.I).strip(" ,;")
    if remainder and not re.fullmatch(
        r"(?:название|дозу|доза|имя|name|dose)\s+(?:не (?:помню|знаю)|неизвестн[ао]|unknown)",
        remainder,
        re.I,
    ):
        if not re.fullmatch(r"[\w\s,.-]+", remainder) or re.search(
            NEGATIVE + "|" + OTHER_SUBJECT, remainder, re.I
        ):
            return messages_with_time
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
        if re.search(VERB, text, re.I):
            if (
                not re.search(VERB + "|" + QUESTION + "|" + NEGATIVE, previous, re.I)
                and re.search(DOSE, previous, re.I)
                and not literal_names(text)
            ):
                verb = re.search(VERB, text, re.I)
                messages_with_time.append(
                    (text[: verb.end()] + " " + previous + " " + text[verb.end() :], now)
                )
            continue
        original = reported_intake_times(previous, stamp, timezone)
        messages_with_time.append((previous, stamp))
        if not original:
            first, separator, rest = previous.partition(",")
            messages_with_time.append(
                (first + " " + text + (separator + rest if separator else ""), now)
            )
    return messages_with_time


def missing_reported_details(event, text, now, timezone, pending):
    """Reject an incomplete extraction that discards a literal dose or named dose."""
    messages = assertion_messages(text, now, timezone, pending)
    for message, stamp in messages:
        message = named_object_order(message, [event])
        sentences = [
            sentence
            for sentence in intake_sentences(unquote_names(message))
            if event.start in reported_intake_times(sentence, stamp, timezone)
        ]
        named = [
            sentence
            for sentence in sentences
            if event.payload.name and event.payload.name.casefold() in literal_names(sentence)
        ]
        for sentence in named or sentences:
            names = literal_names(sentence)
            if names and (event.payload.name is None or event.payload.name.casefold() not in names):
                return True
            if re.search(DOSE, medication_phrase(sentence), re.I):
                if event.payload.dose is None or event.payload.unit is None:
                    return True
                doses = [
                    parse_dose(match[0])
                    for match in re.finditer(DOSE, medication_phrase(sentence), re.I)
                ]
                if (event.payload.dose, event.payload.unit) not in doses:
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


def parse_dose(text):
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(.+)", text)
    unit = match[2].casefold()
    aliases = {"мг": "mg", "мкг": "mcg", "мл": "ml", "г": "g", "ме": "IU", "iu": "IU"}
    unit = aliases.get(unit, unit)
    if unit.startswith(("таблет", "tablet")):
        unit = "tablet"
    if unit.startswith(("кап", "drop")):
        unit = "drop"
    return float(match[1].replace(",", ".")), unit


def invented_unknown_details(event, text, now, timezone, pending):
    for message, stamp in assertion_messages(text, now, timezone, pending):
        for sentence in intake_sentences(named_object_order(message, [event])):
            if event.start in reported_intake_times(sentence, stamp, timezone) and unknown_details(
                event, sentence
            ):
                return True
    return False


def unknown_details(event, text):
    unknown = r"(?:не\s+(?:помню|знаю)|забыл[аи]?|неизвестн\w*|forgot|unknown)"
    qualifier = r"(?:\s+(?:лекарства|препарата|таблетки))?"
    name_unknown = re.search(
        rf"(?:название|имя|name){qualifier}\s+{unknown}|{unknown}\s+(?:название|имя|name)|{UNKNOWN}\s+(?:таблетк|лекарств)",
        text,
        re.I,
    )
    dose_unknown = re.search(
        rf"(?:доз[ауы]|дозировк[ауи]|dose){qualifier}\s+{unknown}|{unknown}\s+(?:(?:название|имя|name)\s+и\s+)?(?:доз[ауы]|дозировк[ауи]|dose)",
        text,
        re.I,
    )
    return bool(
        (name_unknown and event.payload.name is not None)
        or (dose_unknown and event.payload.dose is not None)
        or (
            re.search(
                rf"(?:единиц[ауы](?:\s+измерения)?|units?)\s+{unknown}|{unknown}\s+(?:единиц[ауы](?:\s+измерения)?|units?)",
                text,
                re.I,
            )
            and event.payload.unit is not None
        )
    )
