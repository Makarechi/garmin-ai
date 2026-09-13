"""Conservative literal evidence for newly reported incomplete medication intakes."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

VERB = r"\b(?:принял[аи]?|выпил[аи]?|принимал[аи]?|took|taken)\b"
QUESTION = r"[?]|\b(?:если|бы|например|допустим|цитата|кажется|возможно|наверное|вероятно|обычно|всегда|ежедневно|каждый|каждое|каждую|if|would|suppose|example|maybe|perhaps|probably|think|usually|always|daily|every)\b"
APPROXIMATE = r"\b(?:примерно|около|приблизительно|around|about|approximately)\b"
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
CLOCK = r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})|\bсейчас\b|\bnow\b|\b\d{1,2}:\d{2}\b|\bв\s+\d{1,2}(?::\d{2})?\b"
RELATIVE = rf"\b(?:(?:(?P<n>{QUANTITY})\s+)?(?P<u>{UNIT})|(?P<u2>{UNIT})\s+(?P<n2>{QUANTITY}))\s+(?:назад|ago)\b"
OTHER_SUBJECT = r"\b(?:он|она|они|муж|жена|мама|папа|сын|дочь|реб[её]нок|брат|сестра|he|she|they|husband|wife|mother|father|son|daughter)\b"
DOSE = r"\b\d+(?:[.,]\d+)?\s*(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблет(?:к[ауи]?|ок)|tablets?|кап(?:ля|ли|ель)|drops?)\b"
GENERIC = r"\b(?:таблетк[ауи]|лекарство|medicine|tablets?|pill|я|i|сегодня|вчера|утром|вечером|утра|вечера|дня|ночи|уже|снова|today|yesterday|just|have)\b"
UNKNOWN = r"\b(?:неизвестн\w*|какую-то|какой-то|какие-то|unknown|some)\b"
UNKNOWN_DETAIL = r"\b(?:и\s+)?не\s+(?:помню|знаю)\b"
CLAUSE_COMMA = r"(?<!\d),|,(?!\d)"
CONTRAST = r"(?<![\w-])(?:но|but)(?![\w-])"

MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ),
        1,
    )
}


def normalize_dose_words(text):
    words = "|".join(NUMBERS)
    return re.sub(
        rf"\b({words})(?=\s+(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблет|tablets?|кап|drops?))",
        lambda m: str(NUMBERS[m[1].casefold()]),
        text,
        flags=re.I,
    )


def name_matches(name, names):
    name = name.casefold()
    variants = {name}
    if re.fullmatch(r"[а-яё-]+[бвгджзклмнпрстфхцчшщ]", name):
        variants.update(name + ending for ending in ("а", "у", "ом", "е"))
    if re.fullmatch(r"[а-яё-]+[ая]", name):
        variants.update(
            name[:-1] + ending
            for ending in (("у", "ы", "е", "ой") if name.endswith("а") else ("ю", "и", "е", "ей"))
        )
    return bool(variants.intersection(names))


def calendar_dates(text, now, timezone):
    text = normalize_dose_words(text)
    text = re.sub(
        r",\s*(?:но\s+)?не\s+(?:помню|знаю)\s+(название|дозу|имя)(?=\s*[,.;!?]|\s*$)",
        lambda m: ", " + m[1] + " не помню",
        text,
        flags=re.I,
    )
    text = re.sub(
        r",\s*(?:как обычно|как всегда|as usual|as always)(?=\s*[.!;?]|\s*$)", "", text, flags=re.I
    )
    text = re.sub(r"\bI['’]ve\b", "I have", text, flags=re.I)
    text = re.sub(
        rf"\bне\s+(?:{CLOCK})\s*,?\s*а\s+({CLOCK})", lambda match: match[1], text, flags=re.I
    )

    def english_clock(match):
        hour, minute = int(match[1]), int(match[2] or "0")
        suffix = (match[3] or "").casefold()
        if suffix and 1 <= hour <= 12:
            hour = hour % 12 + (12 if suffix == "pm" else 0)
        return f"{hour:02}:{minute:02}"

    text = re.sub(
        r"\bat\s+(\d{1,2})(?::(\d{2}))?(?:\s*(am|pm))?\b", english_clock, text, flags=re.I
    )
    pattern = r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4})(?:\s+года)?)?\b"

    def replace(match):
        year = int(match[3]) if match[3] else now.astimezone(ZoneInfo(timezone)).year
        for candidate in range(year, year - (1 if match[3] else 9), -1):
            try:
                day = datetime(candidate, MONTHS[match[2].casefold()], int(match[1])).date()
                if match[3] or day <= now.astimezone(ZoneInfo(timezone)).date():
                    return day.isoformat()
            except ValueError:
                continue
        return match[0]

    return re.sub(pattern, replace, text, flags=re.I)


def owner_assertion(clause):
    verb = re.search(VERB, clause, re.I)
    predicate = re.split(UNKNOWN_DETAIL, clause, flags=re.I)[0]
    if verb is None or re.search(NEGATIVE + "|" + OTHER_SUBJECT, predicate, re.I):
        return False
    if (
        re.fullmatch(r"выпил[аи]?", verb[0], re.I)
        and not re.search(
            r"\b(?:таблетк\w*|лекарств\w*|препарат\w*|medicine|pills?|tablets?)\b", clause, re.I
        )
        and not literal_names(clause)
    ):
        return False
    # An unspecified pre-verbal subject is not evidence about the owner.
    prefix = re.sub(RELATIVE, "", clause[: verb.start()], flags=re.I)
    prefix = re.sub(CLOCK, "", prefix, flags=re.I)
    prefix = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", prefix)
    prefix = re.sub(rf"\bчерез\s+{QUANTITY}\s+{UNIT}\b", "", prefix, flags=re.I)
    prefix = re.sub(
        rf"{UNKNOWN}\s+(?:таблетк\w*|лекарств\w*|препарат\w*|drugs?|pills?|tablets?|medicine)\b",
        "",
        prefix,
        flags=re.I,
    )
    prefix = re.sub(
        r"\b(?:после|до|after|before)\s+(?:еды|завтрака|обеда|ужина|food|breakfast|lunch|dinner)\b",
        "",
        prefix,
        flags=re.I,
    )
    prefix = re.sub(GENERIC, "", prefix, flags=re.I)
    prefix = re.sub(r"\b(?:да|yes)\b", "", prefix, flags=re.I)
    return not prefix.strip(" ,;:")


def medication_phrase(sentence):
    verb = re.search(VERB, sentence, re.I)
    if verb is None:
        return ""
    phrase = re.split(
        rf"{CLAUSE_COMMA}|;|{UNKNOWN_DETAIL}|\b(?:после|до|запил[аи]?|after|before|with)\b",
        sentence[verb.end() :],
        flags=re.I,
    )[0]
    return re.sub(rf"\bот\s+.*?(?=(?:{CLOCK})|$)", "", phrase, flags=re.I)


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
            if re.search(
                r"\b(?:таблетк\w*|лекарств\w*|препарат\w*|medicine|pills?|tablets?)\b", tail, re.I
            ):
                return match[0]
            if re.search(OTHER_SUBJECT, match[1], re.I):
                return match[0]
            return match[2] + " " + match[1]

        text = re.sub(pattern, reorder, text, flags=re.I)
    return text


def literal_names(sentence):
    tail = medication_phrase(sentence)
    tail = re.sub(RELATIVE, "", tail, flags=re.I)
    tail = re.sub(rf"(?:{CLOCK})\s+час(?:а|ов)?\b", "", tail, flags=re.I)
    tail = re.sub(CLOCK, "", tail, flags=re.I)
    tail = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", tail)
    tail = re.sub(DOSE, "", tail, flags=re.I)
    tail = re.sub(r"\(\s*\)|\[\s*\]", "", tail)
    tail = re.sub(GENERIC, "", tail, flags=re.I)
    tail = re.sub(r"\b(?:препарат(?:а|ы|ом)?|drugs?)\b", "", tail, flags=re.I)
    tail = re.sub(r"\bот\s+[\w-]+", "", tail, flags=re.I)
    tail = re.sub(UNKNOWN, "", tail, flags=re.I)
    tail = re.sub(rf"\b{QUANTITY}\b", "", tail, flags=re.I)
    return {
        part.strip().casefold()
        for part in re.split(r"\b(?:и|and)\b", tail, flags=re.I)
        if re.fullmatch(r"[\w-]+(?:\s+[\w-]+)*", part.strip())
    }


def distinct_named_intakes(events, text, now, timezone):
    text = calendar_dates(text, now, timezone)
    names = [event.payload.name.casefold() if event.payload.name else None for event in events]
    if len(set(names)) != len(names):
        return False
    matched = set()
    for sentence in intake_sentences(unquote_names(text)):
        if events[0].start in reported_intake_times(sentence, now, timezone):
            matched.update(literal_names(sentence))
            if any(
                re.search(UNKNOWN, part, re.I) and not literal_names(part)
                for part in medication_objects(sentence)
            ):
                matched.add(None)
    return all(
        None in matched if name is None else name_matches(name, matched - {None}) for name in names
    )


def medication_objects(sentence):
    """Keep coordinated medications separate while sharing an explicitly common clock."""
    phrase = medication_phrase(sentence)
    parts = re.split(r"\b(?:и|and)\b", phrase, flags=re.I)
    if len(parts) < 2:
        return [sentence]
    clocks = " ".join(match[0] for match in re.finditer(CLOCK, sentence, re.I))
    return ["принял " + part + " " + clocks for part in parts]


def intake_sentences(text):
    for sentence in re.split(r"(?<=[!?])|[;\n]|\.(?!\d)", text):
        parts = re.split(r"\b(?:и|and)\b|,\s*а\s+", sentence, flags=re.I)
        if (
            len(parts) > 1
            and owner_assertion(parts[0])
            and all(re.search(CLOCK + "|" + RELATIVE, part, re.I) for part in parts)
        ):
            shared_day = re.search(
                r"\b(?:вчера|сегодня|yesterday|today)\b|\b\d{4}-\d{2}-\d{2}\b", parts[0], re.I
            )
            for part in parts:
                if not re.search(VERB, part, re.I):
                    if not re.sub(CLOCK + "|" + RELATIVE, "", part, flags=re.I).strip():
                        shared_object = re.sub(
                            CLOCK + "|" + RELATIVE, "", medication_phrase(parts[0]), flags=re.I
                        ).strip()
                        part = shared_object + " " + part
                    part = "принял " + part
                    if shared_day and not re.search(
                        r"\b(?:вчера|сегодня|yesterday|today)\b|\b\d{4}-\d{2}-\d{2}\b", part, re.I
                    ):
                        part = shared_day[0] + " " + part
                yield part
        elif len(parts) > 1 and any(owner_assertion(part) for part in parts[1:]):
            # Explicit later intake clauses do not inherit a symptom subject.
            for part in parts:
                yield part
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
    if re.search(
        r"\b(?:или|либо|or)\b|\b\d{1,2}:\d{2}\s*(?:[-–—]|до|to)\s*\d{1,2}:\d{2}\b|\b(?:в|с|between)\s+\d{1,2}\s*(?:[-–—]|до|to|and)\s*\d{1,2}\b",
        text,
        re.I,
    ):
        return times
    for match in re.finditer(RELATIVE, text, re.I):
        delta = duration(match["n"] or match["n2"] or "1", match["u"] or match["u2"])
        if delta is not None:
            times.add(now - delta)
    for match in re.finditer(CLOCK, text, re.I):
        value = match[0]
        if value.casefold() == "now":
            value = "сейчас"
        if re.fullmatch(r"в\s+\d{1,2}(?::\d{2})?", value, re.I):
            if re.match(
                r"\s+(?:при[её]м|раз|этап|доз|таблет|кап|мг|мл|mg|ml)", text[match.end() :], re.I
            ):
                continue
            value = value.split()[-1]
            if ":" not in value:
                value += ":00"
        daypart = re.match(
            r"\s+(?:час(?:а|ов)?\s+)?(утра|вечера|дня|ночи)\b", text[match.end() :], re.I
        )
        if daypart and re.fullmatch(r"\d{1,2}:\d{2}", value):
            hour, minute = map(int, value.split(":"))
            if daypart[1].casefold() in {"вечера", "дня"} and 1 <= hour < 12:
                hour += 12
            elif daypart[1].casefold() == "ночи" and 9 <= hour < 12:
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
    text = calendar_dates(text, now, timezone)
    text = unquote_names(text)
    times = set()
    relative = RELATIVE
    # Keep comma-separated unknown-detail qualifiers attached to an intake,
    # but never borrow the clock of a separate symptom or activity assertion.
    for sentence in (
        part
        for assertion in intake_sentences(text)
        for part in re.split(CONTRAST, assertion, flags=re.I)
    ):
        if re.search(QUESTION + r"|\b(?:или|либо|or)\b", sentence, re.I):
            continue
        clauses = re.split(rf"{CLAUSE_COMMA}|;|{CONTRAST}", sentence, flags=re.I)
        anchor = set()
        active = False
        for clause in clauses:
            if re.search(APPROXIMATE, clause, re.I):
                active = False
                continue
            if re.search(VERB, clause, re.I):
                active = owner_assertion(clause)
                if not active:
                    continue
                verb = re.search(VERB, clause, re.I)
                meal_clock = re.search(
                    rf"\b(?:после|до|after|before)\s+(?:завтрака|обеда|ужина|еды|breakfast|lunch|dinner)\s+({CLOCK})",
                    clause,
                    re.I,
                )
                clause = clause[: verb.end()] + medication_phrase(clause)
                if meal_clock:
                    clause += " " + meal_clock[1]
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
    text = calendar_dates(text, now, timezone)
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
            NEGATIVE + "|" + OTHER_SUBJECT, re.sub(UNKNOWN_DETAIL, "", remainder, flags=re.I), re.I
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
        previous = calendar_dates(message.get("text", ""), stamp, timezone)
        correction = re.match(r"\s*(?:нет|no)[,\s]+", previous, re.I)
        if correction:
            messages_with_time = [(text, now)]
            previous = previous[correction.end() :]
        if re.search(VERB, text, re.I):
            if (
                not re.search(VERB + "|" + QUESTION + "|" + NEGATIVE, previous, re.I)
                and (
                    re.search(DOSE, previous, re.I)
                    or (
                        literal_names("принял " + previous)
                        and re.search(
                            r"прин|лекар|таблет|medic",
                            message.get("question", pending.get("question", "")),
                            re.I,
                        )
                    )
                )
                and not literal_names(text)
            ):
                verb = re.search(VERB, text, re.I)
                messages_with_time = [
                    (text[: verb.end()] + " " + previous + " " + text[verb.end() :], now)
                ]
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
    """Require each populated detail to match the same reported medication."""
    candidates = []
    for message, stamp in assertion_messages(text, now, timezone, pending):
        for sentence in intake_sentences(unquote_names(named_object_order(message, [event]))):
            if event.start in reported_intake_times(sentence, stamp, timezone):
                candidates.extend(medication_objects(sentence))
    name = event.payload.name.casefold() if event.payload.name else None
    if name:
        candidates = [
            sentence for sentence in candidates if name_matches(name, literal_names(sentence))
        ]
    else:
        unknown = [
            sentence
            for sentence in candidates
            if not literal_names(sentence) and re.search(UNKNOWN, sentence, re.I)
        ]
        if unknown:
            candidates = unknown
        elif any(literal_names(sentence) for sentence in candidates):
            return True
    if not candidates:
        return True
    for sentence in candidates:
        if unknown_details(event, sentence):
            continue
        dose_text = medication_phrase(sentence)
        dose_text += " " + " ".join(
            clause
            for clause in sentence.split(",")[1:]
            if re.match(r"\s*(?:доза|дозу|дозировка|dose)\b", clause, re.I)
        )
        doses = [parse_dose(match[0]) for match in re.finditer(DOSE, dose_text, re.I)]
        if doses:
            if (event.payload.dose, event.payload.unit) in doses:
                return False
        else:
            numeric_text = re.sub(RELATIVE + "|" + CLOCK, "", dose_text, flags=re.I)
            numeric_text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", numeric_text)
            numbers = [
                float(match[0].replace(",", "."))
                for match in re.finditer(r"\b\d+(?:[.,]\d+)?\b", numeric_text)
            ]
            if numbers:
                if event.payload.dose in numbers and event.payload.unit is None:
                    return False
            elif event.payload.dose is None and event.payload.unit is None:
                return False
    return True


def missing_reported_intakes(events, text, now, timezone, pending):
    expected = set()
    for message, stamp in assertion_messages(text, now, timezone, pending):
        for sentence in intake_sentences(unquote_names(named_object_order(message, events))):
            for at in reported_intake_times(sentence, stamp, timezone):
                for part in medication_objects(sentence):
                    expected.add((at, frozenset(literal_names(part))))
    for at, names in expected:
        if not any(
            event.start == at
            and (
                event.payload.name and name_matches(event.payload.name, names)
                if names
                else event.payload.name is None
            )
            for event in events
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


def unsupported_medication_update(event, fields, previous, text):
    text = normalize_dose_words(unquote_names(text))
    doses = [parse_dose(match[0]) for match in re.finditer(DOSE, text, re.I)]
    for field in ("name", "dose", "unit"):
        value = getattr(event.payload, field)
        if f"payload.{field}" not in fields or value == previous.get(field):
            continue
        if value is None:
            labels = {
                "name": r"название|имя|name",
                "dose": r"доз\w*|dose",
                "unit": r"единиц\w*|unit",
            }[field]
            if not re.search(
                rf"(?:{labels})\s+(?:(?:лекарства|препарата|измерения)\s+)?(?:не помню|не знаю|неизвест\w*|unknown)\b|(?:удали|убери|очисти|remove|clear)\s+(?:{labels})\b",
                text,
                re.I,
            ):
                return True
        elif field == "name":
            if not re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text, re.I):
                return True
        elif field == "dose":
            dose_values = {dose for dose, _ in doses}
            for match in re.finditer(
                r"\b(?:доз[ау]|дозировк[ау]|dose)\s*(?:(?:на|to)\s*)?(\d+(?:[.,]\d+)?)\b",
                text,
                re.I,
            ):
                dose_values.add(float(match[1].replace(",", ".")))
            if value not in dose_values:
                return True
        elif field == "unit":
            units = {unit for _, unit in doses}
            for match in re.finditer(
                r"\b(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблет(?:к[ауи]?|ок)|tablets?|кап(?:ля|ли|ель)|drops?)\b",
                text,
                re.I,
            ):
                units.add(parse_dose("1 " + match[0])[1])
            if value not in units:
                return True
    return False


def invented_unknown_details(event, text, now, timezone, pending):
    for message, stamp in assertion_messages(text, now, timezone, pending):
        for sentence in intake_sentences(named_object_order(message, [event])):
            for part in medication_objects(sentence):
                if event.payload.name and not name_matches(event.payload.name, literal_names(part)):
                    continue
                if event.start in reported_intake_times(
                    sentence, stamp, timezone
                ) and unknown_details(event, part):
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
