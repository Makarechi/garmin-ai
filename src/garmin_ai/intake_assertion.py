"""Conservative literal evidence for newly reported incomplete medication intakes."""

import re
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

VERB = r"\b(?:принял[аи]?|выпил[аи]?|пил[аи]?|принимал[аи]?|проглотил[аи]?|took|taken|swallowed)\b"
QUESTION = r"[?]|\b(?:если|бы|например|допустим|представим|цитата|кажется|возможно|наверное|вероятно|обычно|всегда|ежедневно|каждый|каждое|каждую|if|would|will|shall|буду|будет|собираюсь|планирую|suppose|example|maybe|perhaps|probably|think|usually|always|daily|every)\b"
APPROXIMATE = r"\b(?:примерно|около|приблизительно|around|about|approximately)\b"
NEGATIVE = (
    r"\b(?:не|ничего|нет|no(?![-–—])|not|never|neither|nor|zero|ноль|ни|ли|(?:did|have|has|had|was|were|is|are|do|does)n['’]t)\b"
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
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "one": 1,
    "an": 1,
    "a": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
QUANTITY = r"(?:\d+(?:[.,]\d+)?|один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|one|two|three|four|five|six|seven|eight|nine|ten|an|a)"
UNIT = r"(?:час(?:а|ов)?|минут(?:у|ы)?|hours?|minutes?)"
CLOCK = r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})|\bсейчас\b|\bnow\b|\b\d{1,2}:\d{2}\b|\bв\s+\d{1,2}(?::\d{2})?\b"
RELATIVE = rf"\b(?:(?:(?P<n>{QUANTITY})\s+)?(?P<u>{UNIT})|(?P<u2>{UNIT})\s+(?P<n2>{QUANTITY}))\s+(?:назад|ago)\b"
OTHER_SUBJECT = r"\b(?:он|она|они|муж|жена|мама|папа|сын|дочь|реб[её]нок|брат|сестра|he|she|they|husband|wife|mother|father|son|daughter|врач|доктор|пациент|пациентка|сосед|соседка|коллега|друг|подруга|медсестра|медбрат|фельдшер|санитар|санитарка|doctor|nurse|patient|friend)\b"
DOSE = r"\b\d+(?:[.,]\d+)?\s*(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблет(?:к[ауие]?|ок)|tablets?|pills?|капсул[ауые]?|capsules?|кап(?:ля|ли|ель)|drops?)\b"
GENERIC = r"\b(?:таблетк[ауи]|капсул[ауые]?|capsules?|лекарств[оа]|medications?|meds?|medicines?|tablets?|pills?|я|i|мы|we|сегодня|вчера|утром|вечером|утра|вечера|дня|ночи|свою|свой|свои|сво[её]|мою|мой|мои|мо[её]|my|our|the|уже|снова|ещ[её]|повторно|again|another|today|yesterday|just|already|have)\b"
UNKNOWN = r"\b(?:неизвестн\w*|какую-то|какой-то|какое-то|какие-то|unknown|some)\b"
UNKNOWN_DETAIL = r"\b(?:и\s+)?не\s+(?:помню|знаю)\b|\b(?:I\s+)?(?:do not|don['’]t)\s+(?:remember|know)\s+(?:the\s+)?(?:dose|name|unit)\b"
CLAUSE_COMMA = r"(?<!\d),|,(?!\d)"
REASON = r"\b(?:because|потому\s+что|так\s+как)\b"
COMPLETED_CONTEXT = rf"(?!{VERB})(?:[a-z]+ed(?=\s+[A-Za-z])|read|ran|drove|drank|ate|slept|napped|felt|traveled|travelled|flew|worked|exercised|went|got|came|arrived|попил[аи]?|поел[аи]?|съел[аи]?|спал[аи]?|поспал[аи]?|чувствовал[аи]?|поехал[аи]?|летел[аи]?|работал[аи]?|тренировал[аи]?сь|приш[её]л[аи]?|вернул(?:ся|ась|ись)|добрал(?:ся|ась|ись))"
CONTRAST = r"(?<![\w-])(?:но|but)(?![\w-])"

QUOTED_NAME = r'«[^»]*»|"[^"]*"'


def split_unquoted(pattern, text):
    """Only unquoted punctuation and conjunctions separate assertions."""
    spans = [match.span() for match in re.finditer(QUOTED_NAME, text)]
    start = 0
    parts = []
    for match in re.finditer(pattern, text, re.I):
        if any(left <= match.start() < right for left, right in spans):
            continue
        parts.append(text[start : match.start()])
        start = match.end()
    return parts + [text[start:]]


def strip_approximate_context(text):
    match = re.search(
        rf"{APPROXIMATE}\s+(?:{QUANTITY}\s+)?{UNIT}\s+(?:after|before|после|до)\b",
        text,
        re.I,
    )
    if match and re.search(CLOCK, text[: match.start()], re.I):
        return text[: match.start()]
    return text


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
    for words, unit in (
        (r"milligrams?|миллиграмм(?:а|ов)?", "mg"),
        (r"micrograms?|микрограмм(?:а|ов)?", "mcg"),
        (r"millilit(?:er|re)s?|миллилитр(?:а|ов)?", "ml"),
        (r"grams?|грамм(?:а|ов)?", "g"),
        (r"international\s+units?|международн(?:ых|ые)\s+единиц(?:ы)?", "iu"),
    ):
        text = re.sub(
            rf"(\b{QUANTITY}\s*)({words})\b",
            lambda match, unit=unit: match[1] + unit,
            text,
            flags=re.I,
        )

    # English reports use comma groups for thousands; Russian decimal commas
    # remain decimal separators. Normalize before clause and dose extraction.
    if re.search(r"\b(?:I|took|taken|dose|change|correct)\b", text, re.I) and not re.search(
        r"[а-яё]", text, re.I
    ):
        text = re.sub(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b", lambda m: m[0].replace(",", ""), text)
    text = re.sub(
        r"\b(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)(?=\s+(?:таблет|tablet|мг|mg|мл|ml|кап|drop))",
        lambda m: (
            str(int(m[1] or "0") + int(m[2]) / int(m[3]))
            if sum(len(part or "") for part in m.groups()) < 12 and int(m[3])
            else "?"
        ),
        text,
    )
    text = re.sub(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten)(?=\s+capsules?\b)",
        lambda m: str(NUMBERS[m[1].casefold()]),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(?:half\s+(?:a\s+)?(?=tablets?\b|pills?\b)|половин[ау]\s+(?=таблетки\b))",
        "0.5 ",
        text,
        flags=re.I,
    )

    def named_count(match):
        if re.search(UNKNOWN, match[3], re.I):
            return match[0]
        count = NUMBERS.get(match[2].casefold(), match[2])
        return match[1] + " " + match[3] + " " + str(count) + " " + match[4]

    text = re.sub(
        rf"({VERB})\s+({QUANTITY}|a|an)\s+([\w/-]+(?:\s+[\w/-]+){{0,3}}?)\s+(tablets?|pills?|таблетк[ауи]|таблеток)\b",
        named_count,
        text,
        flags=re.I,
    )
    words = "|".join(NUMBERS)
    return re.sub(
        rf"\b({words})(?=\s+(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблет|tablets?|pills?|кап|drops?))",
        lambda m: str(NUMBERS[m[1].casefold()]),
        text,
        flags=re.I,
    )


def name_word_variants(word):
    forms = {word}
    if re.fullmatch(r"[а-яё-]+[бвгджзклмнпрстфхцчшщ]", word):
        forms.update(word + ending for ending in ("а", "у", "ом", "е"))
    if re.fullmatch(r"[а-яё-]+[ая]", word):
        forms.update(
            word[:-1] + ending
            for ending in (("у", "ы", "е", "ой") if word.endswith("а") else ("ю", "и", "е", "ей"))
        )
    if word.endswith("ая"):
        forms.update(word[:-2] + ending for ending in ("ую", "ой", "ою"))
    elif word.endswith("яя"):
        forms.update(word[:-2] + ending for ending in ("юю", "ей", "ею"))
    return forms


def name_matches(name, names):
    # A space and a hyphen are equivalent before a numeric product designation.
    def tokens(value):
        return re.sub(r"(?<=\w)[ -]+(?=\d)", "-", value.casefold()).split()

    words = tokens(name)
    return any(
        len(tokens(candidate)) == len(words)
        and all(
            literal in name_word_variants(word)
            for word, literal in zip(words, tokens(candidate), strict=True)
        )
        for candidate in names
    )


def calendar_dates(text, now, timezone):
    text = normalize_dose_words(text)
    text = re.sub(
        r"\b(?:(?:but|and)\s+)?I\s+(?:do not|don['’]t)\s+(?:remember|know)\s+(?:the\s+)?(dose|name|unit)\b",
        lambda m: m[1] + " unknown",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(?:at\s+(noon|midnight)|в\s+(полдень|полночь))\b",
        lambda m: "12:00" if (m[1] or m[2]).casefold() in {"noon", "полдень"} else "00:00",
        text,
        flags=re.I,
    )
    english_months = "january february march april may june july august september october november december".split()
    russian_months = list(MONTHS)

    def english_date(match):
        month, day, year = match.groups()
        return (
            day
            + " "
            + russian_months[english_months.index(month.casefold())]
            + (" " + year if year else "")
        )

    text = re.sub(
        r"\b(?:on\s+)?("
        + "|".join(english_months)
        + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
        english_date,
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+("
        + "|".join(english_months)
        + r")(?:\s+(\d{4}))?\b",
        lambda m: (
            m[1]
            + " "
            + russian_months[english_months.index(m[2].casefold())]
            + (" " + m[3] if m[3] else "")
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        rf"\b(I|we)\s+had\s+(?={DOSE}\s+of\s+[\w])",
        lambda m: m[1] + " took ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\bI\s+did\s+take\b", "I took", text, flags=re.I)
    text = re.sub(r"\bI(?:\s+had|['’]d)\s+taken\b", "I taken", text, flags=re.I)
    text = re.sub(
        rf"\bI\s+(?:forgot|(?:do not|don['’]t)\s+(?:remember|know))\s+what\s+I\s+({VERB})",
        lambda m: "I " + m[1] + " unknown medication",
        text,
        flags=re.I,
    )
    text = re.sub(
        rf"\bя\s+(?:забыл[а]?|не\s+(?:помню|знаю))\s*,?\s*(?:какую\s+таблетку|какое\s+лекарство|что)\s+(?:я\s+)?({VERB})",
        lambda m: "я " + m[1] + " неизвестное лекарство",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b(?:with\s+water|запил[аи]?\s+водой)\b", "", text, flags=re.I)
    text = re.sub(
        rf"(^|[.;!]\s*)((?:название|имя|доз[ауы]|name|dose)(?:\s+(?:лекарства|препарата))?\s+(?:не помню|не знаю|unknown))\s*,\s*(?:(?:но|but)\s+)?([^.;!\n]*{VERB}[^.;!\n]*)",
        lambda m: m[1] + m[3] + ", " + m[2],
        text,
        flags=re.I,
    )
    text = re.sub(rf"({DOSE})\s+({VERB})", lambda m: m[2] + " " + m[1], text, flags=re.I)
    text = re.sub(r"\b((?i:витамин))\s+[ВB]\s+(\d+)\b", lambda m: m[1] + " В" + m[2], text)
    text = re.sub(
        r"\bне помню,\s*(?:какую|какой)\s+(таблетку|препарат)",
        r"не помню, неизвестный \1",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bне\s+([^,;.!?]+),\s*а\s+",
        lambda m: m[0] if re.search(VERB, m[1], re.I) else "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r",\s*(?:но\s+)?не\s+(?:помню|знаю)\s+(название|дозу|имя)(?=\s*[,.;!?]|\s*$)",
        lambda m: ", " + m[1] + " не помню",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"(?:,\s*|\s+)(?:как обычно|как всегда|as usual|as always)(?=\s*[.!;?]|\s*$)",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\bI['’]ve\b", "I have", text, flags=re.I)
    text = re.sub(
        rf"\bне\s+(?:{CLOCK})\s*,?\s*а\s+({CLOCK})", lambda match: match[1], text, flags=re.I
    )

    text = re.sub(
        r"\b(утром|вечером|дн[её]м|ночью)\s+в\s+(\d{1,2}(?::\d{2})?)\b",
        lambda m: (
            "в "
            + m[2]
            + " "
            + {"утром": "утра", "вечером": "вечера", "днем": "дня", "днём": "дня", "ночью": "ночи"}[
                m[1].casefold()
            ]
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(?:this\s+)?(morning|afternoon|evening|night)\s+at\s+(\d{1,2}(?::\d{2})?)\b",
        lambda m: "at " + m[2] + " in the " + m[1],
        text,
        flags=re.I,
    )

    def english_daypart(match):
        hour = int(match[1])
        period = match[3].casefold()
        if not 1 <= hour <= 12:
            return "?"
        suffix = (
            "am"
            if period == "morning" or (period == "night" and (hour <= 6 or hour == 12))
            else "pm"
        )
        return "at " + match[1] + (":" + match[2] if match[2] else "") + " " + suffix

    text = re.sub(
        r"\bat\s+(\d{1,2})(?::(\d{2}))?\s+(?:in\s+(?:the\s+)?|at\s+)(morning|afternoon|evening|night)\b",
        english_daypart,
        text,
        flags=re.I,
    )

    def english_clock(match):
        hour, minute = int(match[1]), int(match[2] or "0")
        suffix = (match[3] or "").casefold()
        if suffix and not 1 <= hour <= 12:
            return "?"
        if suffix and 1 <= hour <= 12:
            hour = hour % 12 + (12 if suffix == "pm" else 0)
        return f"{hour:02}:{minute:02}"

    text = re.sub(
        r"\bat\s+(\d{1,2})(?::(\d{2}))?(?:\s*(am|pm))?\b", english_clock, text, flags=re.I
    )
    pattern = (
        r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4})(?:\s+г(?:ода|\.(?!\w)|\b))?)?"
    )

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

    text = re.sub(pattern, replace, text, flags=re.I)
    text = re.sub(
        r"\b(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}[+-]\d{2}:\d{2})\b",
        lambda m: m[1] + "T" + m[2],
        text,
    )
    return text


def owner_assertion(clause):
    predicate = re.split(UNKNOWN_DETAIL + "|" + REASON, clause, flags=re.I)[0]
    verb = re.search(VERB, predicate, re.I)
    if verb:
        predicate = (
            predicate[: verb.end()]
            + re.split(
                r"\b(?:with|without|с|без)\b", predicate[verb.end() :], maxsplit=1, flags=re.I
            )[0]
        )
    if (
        verb
        and re.search(r"\b(?:таблетк\w*|лекарств\w*|препарат\w*)\b", clause[: verb.start()], re.I)
        and not re.search(r"\bя\b", clause[: verb.start()], re.I)
    ):
        if re.match(r"\s+[А-ЯЁ][а-яё]+\b", clause[verb.end() :]):
            return False
    if re.search(
        rf"{VERB}\s+(?:(?:a|an|the)\s+)?(?:taxi|train|bus|flight|plane|subway|tram|такси|поезд|автобус|душ|решение|ванну|участие|звонок|вызов|it\s+easy|photo|picture|selfie|screenshot|exam|test|seat|look|breath|chance|risk|step|notes|care|shower|bath|decision|walk|break|part|call|nap)\b",
        clause,
        re.I,
    ):
        return False
    subject_scope = (
        predicate[: verb.start()]
        if verb and re.search(r"\b(?:я|I|мы|we)\b", predicate[: verb.start()], re.I)
        else predicate
    )
    if (
        verb is None
        or re.search(NEGATIVE, predicate, re.I)
        or re.search(
            r"\b0(?:[.,]0+)?\s+(?:[\w/-]+\s+)?(?:tablets?|pills?|таблетк\w*|капсул\w*)\b",
            predicate,
            re.I,
        )
        or re.search(OTHER_SUBJECT, subject_scope, re.I)
    ):
        return False
    if (
        re.fullmatch(r"(?:вы)?пил[аи]?", verb[0], re.I)
        and not re.search(
            r"\b(?:таблетк\w*|лекарств\w*|препарат\w*|medicine|pills?|tablets?)\b", clause, re.I
        )
        and (
            not literal_names(clause)
            or literal_names(clause)
            & {
                "кофе",
                "чай",
                "чаю",
                "воду",
                "вода",
                "сок",
                "молоко",
                "пиво",
                "вино",
                "колу",
                "какао",
            }
        )
    ):
        return False
    # An unspecified pre-verbal subject is not evidence about the owner.
    prefix = clause[: verb.start()]
    prefix = re.sub(
        r"^\s*(?:после|до|after|before)\s+[\w -]+\s+(?:я|I|мы|we)\s*$",
        "",
        prefix,
        flags=re.I,
    )
    prefix = re.sub(RELATIVE, "", prefix, flags=re.I)
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
    sentence = strip_approximate_context(sentence)
    sentence = re.sub(r"\b(?:with\s+water|запил[аи]?\s+водой)\b", "", sentence, flags=re.I)
    sentence = re.sub(
        r"\b(?:после|до|с|after|before|with)\s+(?:еды|едой|завтрака|обеда|ужина|food|breakfast|lunch|dinner)\b",
        "",
        sentence,
        flags=re.I,
    )
    verb = re.search(VERB, sentence, re.I)
    if verb is None:
        return ""
    phrase = re.split(
        rf"{CLAUSE_COMMA}|;|{UNKNOWN_DETAIL}|{REASON}|\b(?:после|до|запил[аи]?|after|before|with)\b",
        sentence[verb.end() :],
        flags=re.I,
    )[0]
    phrase = re.sub(r"\bс\s+(?:водой|едой)\b", "", phrase, flags=re.I)
    return re.sub(rf"\b(?:от|for)\s+.*?(?=(?:{CLOCK})|(?:{DOSE})|$)", "", phrase, flags=re.I)


def named_object_order(text, events):
    for event in events:
        name = getattr(event.payload, "name", None)
        if not name:
            continue
        name_pattern = r"\s+".join(
            "(?:"
            + "|".join(re.escape(form) for form in sorted(name_word_variants(word.casefold())))
            + ")"
            for word in name.split()
        )
        pattern = rf"\b({name_pattern})\s+({VERB})(?!\s+(?:таблетк|лекарств|medicine|pill|tablet))"

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


def bare_dose_reported(sentence):
    return bool(
        re.search(
            r"\b(?:доз[ауы]|дозировк[ауи]|dose|единиц[ауы](?:\s+измерения)?|unit)\b", sentence, re.I
        )
    )


def literal_names(sentence):
    quoted = {}
    token_prefix = "quotedmedication"
    while token_prefix in sentence.casefold():
        token_prefix += "x"

    def protect(match):
        key = token_prefix + str(len(quoted))
        quoted[key] = match[0][1:-1].casefold()
        return key

    sentence = re.sub(QUOTED_NAME, protect, sentence)
    tail = medication_phrase(sentence)
    tail = re.sub(r"\b(?:twice|дважды)\b", "", tail, flags=re.I)
    tail = re.sub(r"^\s*(?:both|then|затем|потом)\b\s*", "", tail, flags=re.I)
    count_words = "|".join(word for word in NUMBERS if word not in {"a", "an"})
    tail = re.sub(
        r"\b(?:"
        + "|".join(NUMBERS)
        + rf")(?=\s+(?:{UNKNOWN}\s+)?(?:таблетк[ауи]|tablets?|pills?|capsules?|капсул[ауые]?)\b)",
        "",
        tail,
        flags=re.I,
    )
    if re.search(r"\b(?:or|или|либо)\b", tail, re.I):
        return set()
    tail = re.sub(RELATIVE, "", tail, flags=re.I)
    tail = re.sub(rf"(?:{CLOCK})\s+час(?:а|ов)?\b", "", tail, flags=re.I)
    tail = re.sub(CLOCK, "", tail, flags=re.I)
    tail = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", tail)
    tail = re.sub(rf"(?:\bпо\s+)?(?:{DOSE})", "", tail, flags=re.I)
    tail = re.sub(r"\(\s*\)|\[\s*\]", "", tail)
    tail = re.sub(GENERIC, "", tail, flags=re.I)
    tail = re.sub(r"^\s*of\s+", "", tail, flags=re.I)
    tail = re.sub(r"\b(?:препарат(?:а|ы|ом)?|drugs?)\b", "", tail, flags=re.I)
    tail = re.sub(r"\bот\s+[\w-]+", "", tail, flags=re.I)
    tail = re.sub(UNKNOWN, "", tail, flags=re.I)
    if bare_dose_reported(sentence):
        tail = re.sub(r"\b\d+(?:[.,]\d+)?\b", "", tail)
    tail = re.sub(rf"^\s*(?:{count_words}|\d+(?:[.,]\d+)?)\s+", "", tail, flags=re.I)
    tail = re.sub(r"^\s*(?:a|an)\s+", "", tail, flags=re.I)
    tail = tail.strip(" .!;:()[]")
    return {
        quoted.get(part.strip().casefold(), part.strip().casefold())
        for part in re.split(r"\b(?:и|and)\b", tail, flags=re.I)
        if re.fullmatch(
            r"[\w-]+(?:[/\u2019\u0027][\w-]+)*(?:\s+[\w-]+(?:[/\u2019\u0027][\w-]+)*)*",
            part.strip(),
        )
    }


def distinct_named_intakes(events, text, now, timezone):
    text = calendar_dates(text, now, timezone)
    available = []
    for sentence in intake_sentences(unquote_names(text)):
        if events[0].start in reported_intake_times(sentence, now, timezone):
            available.extend(literal_names(part) for part in medication_objects(sentence))
    for event in events:
        for index, names in enumerate(available):
            if name_matches(event.payload.name, names) if event.payload.name else not names:
                available.pop(index)
                break
        else:
            return False
    return True


def medication_objects(sentence):
    """Keep coordinated medications separate while sharing an explicitly common clock."""
    phrase = medication_phrase(sentence)
    parts = split_unquoted(r"\b(?:и|and)\b", phrase)
    if len(parts) < 2:
        return [sentence]
    clocks = " ".join(match[0] for match in re.finditer(CLOCK, sentence, re.I))
    shared = re.search(rf"\bпо\s+({DOSE})", parts[-1], re.I)
    result = []
    qualifiers = split_unquoted(CLAUSE_COMMA, sentence)[1:]
    for part in parts:
        assertion = (
            "принял "
            + part
            + (" " + shared[1] if shared and not re.search(DOSE, part, re.I) else "")
            + " "
            + clocks
        )
        for qualifier in qualifiers:
            match = re.fullmatch(
                rf"\s*(.+?)\s+(?:dose|доза|дозу|дозировка)\s+({DOSE})\s*[.!]?",
                qualifier,
                re.I,
            )
            if match and name_matches(match[1].strip(), literal_names("took " + part)):
                assertion += ", dose " + match[2]
        result.append(assertion)
    return result


def intake_sentences(text):
    # An explicitly coordinated first-person subject includes the owner.
    companion = rf"(?:(?:my|our|the|моя|мой)\s+)?{OTHER_SUBJECT}"
    text = re.sub(
        rf"\b{companion}\s+(?:and|и)\s+(I|я)\s+(?={VERB})",
        lambda m: m[1] + " ",
        text,
        flags=re.I,
    )
    text = re.sub(
        rf"\b(I|я)\s+(?:and|и)\s+{companion}\s+(?={VERB})",
        lambda m: m[1] + " ",
        text,
        flags=re.I,
    )

    def coadminister(match):
        if any(
            quoted.start() <= match.start() < quoted.end()
            for quoted in re.finditer(QUOTED_NAME, text)
        ):
            return match[0]
        prefix = text[: match.start()]
        tail = split_unquoted(r"[;.!?\n]", text[match.end() :])[0]
        adjunct = re.match(
            rf"\s*(?:{OTHER_SUBJECT}|{NEGATIVE}|my\b|our\b|water\b|food\b|milk\b|lunch\b|breakfast\b|dinner\b)",
            tail,
            re.I,
        )
        if re.search(VERB, prefix, re.I) and not adjunct and literal_names("took " + tail):
            return "and "
        return match[0]

    text = re.sub(r"\bwith\s+", coadminister, text, flags=re.I)
    text = re.sub(
        r"\b(например|допустим|представим|for example|suppose)[.!:]\s*", r"\1 ", text, flags=re.I
    )
    discourse_subject = None
    for sentence in split_unquoted(
        rf"(?<=[!?])|[;\n]|\.(?!\d)|{CONTRAST}|,\s*(?:хотя|although|though)\b|\b(?:after|before|после того как|до того как)\s+(?=(?:(?:I|я|we|мы)\s+)?{VERB})",
        text,
    ):
        first_verb = re.search(VERB, sentence, re.I)
        prefix = sentence[: first_verb.start()] if first_verb else sentence
        if re.search(r"\b(?:I|я|we|мы)\b", prefix, re.I):
            discourse_subject = None
        elif subject := re.match(rf"\s*({OTHER_SUBJECT})", prefix, re.I):
            discourse_subject = subject[1]
        elif discourse_subject and first_verb:
            sentence = discourse_subject + " " + sentence
        sentence = re.sub(
            rf"^\s*({CLOCK})\s*,\s*(?=(?:(?:I|я|we|мы)\s+)?{VERB})",
            lambda m: m[1] + " ",
            sentence,
            flags=re.I,
        )
        comma_objects = split_unquoted(CLAUSE_COMMA, sentence)
        if len(comma_objects) > 1 and owner_assertion(comma_objects[0]):
            coordinated = comma_objects[0]
            for part in comma_objects[1:]:
                # An Oxford comma does not terminate the coordinated list.
                if re.match(r"\s*and\b", part, re.I):
                    part = re.sub(r"^\s*and\b\s*", "", part, flags=re.I)
                bare_name = literal_names("took " + part)
                context = re.search(
                    rf"{VERB}|{UNKNOWN_DETAIL}|{NEGATIVE}|{QUESTION}|{OTHER_SUBJECT}|{COMPLETED_CONTEXT}|{APPROXIMATE}|{REASON}|\b(?:dose|name|unit|доз\w*|название|имя|единиц\w*|мигрень|головная|migraine|headache|sleep|coffee|сон|кофе|but|но|исправ\w*|измени\w*|уточни\w*|change|correct)\b",
                    part,
                    re.I,
                )
                coordinated += (
                    " and "
                    if bare_name and not context and not re.match(r"\s*(?:а|и|and)\b", part, re.I)
                    else ", "
                ) + part
            sentence = coordinated
        parts = split_unquoted(r"\b(?:и|and)\b|,\s*а\s+", sentence)
        if len(parts) > 1 and owner_assertion(parts[0]):
            if any(
                re.match(rf"\s*(?:(?:I|я|we|мы)\s+)?{COMPLETED_CONTEXT}\b", part, re.I)
                for part in parts[1:]
            ):
                medication_parts = []
                for part in parts:
                    if re.match(rf"\s*(?:(?:I|я|we|мы)\s+)?{COMPLETED_CONTEXT}\b", part, re.I):
                        if medication_parts:
                            yield from intake_sentences(" and ".join(medication_parts))
                            medication_parts = []
                        yield part
                    else:
                        medication_parts.append(part)
                if medication_parts:
                    yield from intake_sentences(" and ".join(medication_parts))
                continue
            first_verb = re.search(VERB, parts[0], re.I)
            if (
                re.search(CLOCK + "|" + RELATIVE, parts[0], re.I)
                and not re.search(CLOCK + "|" + RELATIVE, parts[0][: first_verb.start()], re.I)
                and any(
                    not re.search(CLOCK + "|" + RELATIVE, part, re.I)
                    and literal_names("took " + part)
                    and not re.search(UNKNOWN_DETAIL, part, re.I)
                    for part in parts[1:]
                )
            ):
                yield parts[0]
                for part in parts[1:]:
                    yield part if re.search(VERB, part, re.I) else "принял " + part
                continue
            if not re.search(CLOCK + "|" + RELATIVE, parts[0], re.I) and any(
                re.match(rf"\s*(?:{CLOCK})", part, re.I) for part in parts[1:]
            ):
                yield parts[0]
                for part in parts[1:]:
                    yield part if re.search(VERB, part, re.I) else "принял " + part
                continue
        comma_parts = split_unquoted(rf"\b(?:и|and)\b|,\s*а\s+|{CLAUSE_COMMA}", sentence)
        if owner_assertion(comma_parts[0]) and all(
            re.search(CLOCK + "|" + RELATIVE, part, re.I) for part in comma_parts
        ):
            parts = comma_parts
        if (
            len(parts) > 1
            and owner_assertion(parts[0])
            and all(re.search(CLOCK + "|" + RELATIVE, part, re.I) for part in parts)
        ):
            shared_day = re.search(
                r"\b(?:вчера|сегодня|yesterday|today)\b|\b\d{4}-\d{2}-\d{2}\b", parts[0], re.I
            )
            for part in parts:
                part = re.sub(r"^\s*(?:then|затем|потом)\b\s*", "", part, flags=re.I)
                if not re.search(VERB, part, re.I):
                    # A completed non-medication predicate supplies its own
                    # event type; only bare medication objects inherit intake.
                    if re.match(
                        rf"\s*(?:(?:I|я|we|мы)\s+)?{COMPLETED_CONTEXT}\b",
                        part,
                        re.I,
                    ):
                        yield part
                        continue
                    subject = re.split(r"\b(?:от|for)\b", part, flags=re.I)[0]
                    if re.search(
                        r"\b(?:мигрень|мигрени|головная\s+боль|migraine|headache|кофе|coffee|сон|nap|sleep|тренировка|workout|meal|lunch|breakfast|dinner|обед|завтрак|ужин|поел[аи]?|съел[аи]?|ate)\b",
                        subject,
                        re.I,
                    ):
                        yield part
                        continue
                    if not re.sub(CLOCK + "|" + RELATIVE, "", part, flags=re.I).strip():
                        shared_object = re.sub(
                            CLOCK + "|" + RELATIVE + r"|\b(?:twice|дважды)\b",
                            "",
                            medication_phrase(parts[0]),
                            flags=re.I,
                        ).strip()
                        part = shared_object + " " + part
                    part = "принял " + part
                    if shared_day and not re.search(
                        r"\b(?:вчера|сегодня|yesterday|today)\b|\b\d{4}-\d{2}-\d{2}\b", part, re.I
                    ):
                        part = shared_day[0] + " " + part
                yield part
        elif len(parts) > 1 and any(owner_assertion(part) for part in parts[1:]):
            # A shared third-party subject survives conjunctions; an explicit
            # first-person clause establishes a new subject.
            subject = None
            for part in parts:
                explicit = re.search(OTHER_SUBJECT, part, re.I)
                if re.search(r"\b(?:я|I|мы|we)\b", part, re.I):
                    subject = None
                elif explicit:
                    subject = explicit[0]
                else:
                    leading = re.match(
                        r"\s*(?:(?:my|our|the|a|an|мой|моя|наш|наша)\s+)?([A-ZА-ЯЁ][\w-]+)\b",
                        part,
                        re.I,
                    )
                    if leading and not re.fullmatch(
                        VERB
                        + "|"
                        + GENERIC
                        + "|"
                        + r"мигрень|головная|после|до|after|before|"
                        + COMPLETED_CONTEXT,
                        leading[1],
                        re.I,
                    ):
                        subject = leading[1]
                    elif subject:
                        part = subject + " " + part
                yield part
        else:
            yield sentence


def unquote_names(text):
    # A quoted verb is not an assertion by the sender; quoted names are fine.
    return re.sub(r'«[^»]*»|"[^"]*"', lambda m: "" if re.search(VERB, m[0], re.I) else m[0], text)


def alternative_times(text):
    candidate = rf"(?:{CLOCK}|{QUANTITY}(?:\s+{UNIT})?|{UNIT})"
    return bool(
        re.search(rf"{candidate}\s+(?:или|либо|or)\s+(?:(?:в|at)\s+)?{candidate}", text, re.I)
    )


def explicit_times(text, now, timezone):
    from garmin_ai.diary_forms import form_time

    times = set()
    range_text = re.sub(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?[+-]\d{2}:\d{2}\b", "", text)
    if alternative_times(text) or re.search(
        r"\b\d{1,2}(?::\d{2})?\s+(?:или|либо|or)\s+(?:в\s+)?\d{1,2}\b|\b\d{1,2}:\d{2}\s*(?:[-–—]|до|to)\s*\d{1,2}(?::\d{2})?\b|\b(?:в|с|between)\s+\d{1,2}\s*(?:[-–—]|до|to|and)\s*\d{1,2}\b",
        range_text,
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
    if re.fullmatch(r"\d+(?:[.,]\d+)?", raw) and len(raw) > 12:
        return None
    count = float(raw.replace(",", ".")) if re.fullmatch(r"\d+(?:[.,]\d+)?", raw) else NUMBERS[raw]
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
        part for assertion in intake_sentences(text) for part in split_unquoted(CONTRAST, assertion)
    ):
        first_verb = re.search(VERB, sentence, re.I)
        if first_verb and re.search(QUESTION, sentence[: first_verb.start()], re.I):
            continue
        clauses = split_unquoted(rf"{CLAUSE_COMMA}|;|{CONTRAST}", sentence)
        anchor = set()
        active = False
        for clause in clauses:
            if re.search(QUESTION, split_unquoted(REASON, clause)[0], re.I):
                active = False
                continue
            clause = strip_approximate_context(clause)
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
                if alternative_times(clause) or re.search(
                    r"\b(?:or|или|либо)\b", medication_phrase(clause), re.I
                ):
                    continue
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
                known_dose = re.fullmatch(
                    rf"(?:(?:доза|дозу|дозировка|dose)\s+)?(?:{DOSE})", remainder, re.I
                )
                if (
                    not remainder
                    or known_dose
                    or re.fullmatch(
                        r"(?:название|дозу|доза|имя|name|dose)\s+(?:не (?:помню|знаю)|неизвестн[ао]|unknown)",
                        remainder,
                        re.I,
                    )
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
            stamp = datetime.fromisoformat(message.get("at") or pending["created_at"])
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
        if (
            original
            and not explicit_times(text, now, timezone)
            and not re.search(UNKNOWN_DETAIL, text, re.I)
        ):
            first, separator, rest = previous.partition(",")
            messages_with_time.append(
                (first + " " + text + (separator + rest if separator else ""), stamp)
            )
            continue
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
            or re.fullmatch(rf"\s*(?:{DOSE})\s*[.!]?", clause, re.I)
        )
        doses = [parse_dose(match[0]) for match in re.finditer(DOSE, dose_text, re.I)]
        if doses:
            if len(set(doses)) > 1:
                continue  # The payload cannot represent count plus strength.
            if (event.payload.dose, event.payload.unit) in doses:
                return False
        else:
            numeric_text = re.sub(RELATIVE + "|" + CLOCK, "", dose_text, flags=re.I)
            numeric_text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", numeric_text)
            numbers = [
                float(match[0].replace(",", "."))
                for match in re.finditer(r"\b\d+(?:[.,]\d+)?\b", numeric_text)
                if bare_dose_reported(sentence)
            ]
            count_words = "|".join(word for word in NUMBERS if word not in {"a", "an"})
            count = re.match(
                rf"\s*({count_words}|\d+(?:[.,]\d+)?)\s+(?=[\w])",
                medication_phrase(sentence),
                re.I,
            )
            if count and literal_names(sentence) and not re.search(UNKNOWN, sentence, re.I):
                raw = count[1].casefold()
                numbers.append(
                    float(NUMBERS[raw]) if raw in NUMBERS else float(raw.replace(",", "."))
                )
            if numbers:
                if event.payload.dose in numbers and event.payload.unit is None:
                    return False
            elif event.payload.dose is None:
                known_units = {
                    parse_dose("1 " + match[1])[1]
                    for match in re.finditer(
                        r"\b(?:единиц[ауы](?:\s+измерения)?|unit)\s+(мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблетк[ауи]|tablet|капли|drop)\b",
                        sentence,
                        re.I,
                    )
                }
                if event.payload.unit in known_units or (
                    not known_units and event.payload.unit is None
                ):
                    return False
    return True


def without_target_restatement(text, event, now, timezone):
    kept = []
    removed = False
    for sentence in intake_sentences(calendar_dates(text, now, timezone)):
        if (
            not removed
            and re.search(r"\b(?:исправ\w*|измени\w*|уточни\w*|correct|change)\b", sentence, re.I)
            and not re.search(r"\b(?:ещ[её]|снова|повторно|another|again)\b", sentence, re.I)
            and event.start in reported_intake_times(sentence, now, timezone)
            and not missing_reported_details(event, sentence, now, timezone, None)
        ):
            removed = True
        else:
            kept.append(sentence)
    return ";".join(kept)


def missing_reported_intakes(events, text, now, timezone, pending):
    # A repetition marker requires separate explicit clocks, rather than
    # silently accepting just one of the reported occurrences.
    for sentence in split_unquoted(r"[;!\n]|\.(?!\d)", calendar_dates(text, now, timezone)):
        unquoted = re.sub(QUOTED_NAME, "", sentence)
        if (
            re.search(r"\b(?:twice|дважды)\b", unquoted, re.I)
            and owner_assertion(sentence)
            and len(reported_intake_times(sentence, now, timezone)) != 2
        ):
            return True
    # A new untimed assertion cannot disappear behind another extracted event.
    # Detail-only replies may still complete an earlier pending assertion.
    if not pending or re.search(VERB, text, re.I):
        for sentence in intake_sentences(unquote_names(calendar_dates(text, now, timezone))):
            if (
                owner_assertion(sentence)
                and not re.search(QUESTION, sentence, re.I)
                and not reported_intake_times(sentence, now, timezone)
            ):
                return True
    for sentence in intake_sentences(unquote_names(calendar_dates(text, now, timezone))):
        verb = re.search(VERB, sentence, re.I)
        if (
            not verb
            or owner_assertion(sentence)
            or re.search(QUESTION + "|" + NEGATIVE + "|" + OTHER_SUBJECT, sentence, re.I)
        ):
            continue
        prefix = sentence[: verb.start()].strip()
        tail = sentence[verb.end() :]
        remainder = re.sub(CLOCK + "|" + RELATIVE + "|" + GENERIC, "", tail, flags=re.I).strip(
            " .!;:"
        )
        if prefix and not remainder and explicit_times(tail, now, timezone):
            if not any(
                event.payload.name and name_matches(event.payload.name, {prefix})
                for event in events
            ):
                return True
    expected = Counter()
    stamps = {}
    for message, stamp in assertion_messages(text, now, timezone, pending):
        reported = Counter()
        for sentence in intake_sentences(unquote_names(named_object_order(message, events))):
            for at in reported_intake_times(sentence, stamp, timezone):
                for part in medication_objects(sentence):
                    identity = (at, frozenset(literal_names(part)), part.casefold())
                    reported[identity] += 1
                    stamps[identity] = stamp
        # A clarification may repeat earlier evidence. Preserve the largest
        # explicit multiplicity in a message, without counting history twice.
        expected |= reported
    available = list(events)
    for at, names, assertion in expected.elements():
        for index, event in enumerate(available):
            if (
                event.start == at
                and (name_matches(event.payload.name, names) if event.payload.name else not names)
                and not missing_reported_details(
                    event, assertion, stamps[(at, names, assertion)], timezone, None
                )
            ):
                available.pop(index)
                break
        else:
            return True
    return False


def parse_dose(text):
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(.+)", text)
    unit = match[2].casefold()
    aliases = {"мг": "mg", "мкг": "mcg", "мл": "ml", "г": "g", "ме": "IU", "iu": "IU"}
    unit = aliases.get(unit, unit)
    if unit.startswith(("таблет", "tablet", "pill")):
        unit = "tablet"
    if unit.startswith(("капсул", "capsule")):
        unit = "capsule"
    elif unit.startswith(("кап", "drop")):
        unit = "drop"
    return float(match[1].replace(",", ".")), unit


def unsupported_medication_update(event, fields, previous, text):
    text = normalize_dose_words(unquote_names(text))
    text = " ".join(
        clause
        for clause in split_unquoted(rf"[;\n]|\.(?!\d)|\b(?:и|and)\b|{CONTRAST}", text)
        if not re.search(
            r"\b(?:не|not|never|do not|don['’]t)\s+(?:\w+\s+){0,2}(?:change|correct|clear|remove|исправ\w*|измени\w*|уточни\w*|удаля\w*|удали|убери|очисти)\b",
            re.sub(QUOTED_NAME, "", clause),
            re.I,
        )
        and (
            not re.search(VERB, clause, re.I)
            or (
                re.search(r"\b(?:исправ\w*|измени\w*|уточни\w*|correct|change)\b", clause, re.I)
                and any(
                    name and name_matches(name, literal_names(clause))
                    for name in (event.payload.name, previous.get("name"))
                )
            )
        )
    )
    text = re.sub(
        r"\b\d{1,2}\s+(?:" + "|".join(MONTHS) + r")\s+\d{4}\s*г(?:ода|\.)?\b", "", text, flags=re.I
    )
    text = re.sub(
        rf"\b(?:from|с|от)\s+{DOSE}\s+(?:to|на)\s+({DOSE})",
        lambda m: "to " + m[1],
        text,
        flags=re.I,
    )
    doses = [parse_dose(match[0]) for match in re.finditer(DOSE, text, re.I)]
    for field in ("name", "dose", "unit"):
        labels = {
            "name": r"название|имя|name",
            "dose": r"доз\w*|dose",
            "unit": r"единиц\w*|unit",
        }[field]
        requested = bool(re.search(rf"\b(?:{labels})\b", text, re.I)) or (
            field in {"dose", "unit"} and bool(doses)
        )
        # apply_command only persists listed fields. Validate the value that
        # will actually survive, including explicitly requested omitted fields.
        if f"payload.{field}" not in fields:
            if not requested:
                continue
            value = previous.get(field)
        else:
            value = getattr(event.payload, field)
        if value == previous.get(field) and not requested:
            continue
        if value is None:
            labels = {
                "name": r"название|имя|name",
                "dose": r"доз\w*|dose",
                "unit": r"единиц\w*|unit",
            }[field]
            if not re.search(
                rf"(?:{labels})\s+(?:(?:лекарства|препарата|измерения)\s+)?(?:(?:на|to)\s+)?(?:не помню|не знаю|неизвест\w*|unknown)\b|(?:удали|убери|очисти|remove|clear)\s+(?:{labels})\b",
                text,
                re.I,
            ):
                return True
        elif field == "name":
            names = set()
            for clause in split_unquoted(r"[;\n]|\.(?!\d)|,|\b(?:и|and)\b", text):
                if re.search(VERB, clause, re.I):
                    names.update(literal_names(clause))
                match = re.search(
                    r"\b(?:(?:название|имя|name)(?:\s+(?:лекарства|препарата|таблетки|medication|medicine|drug))?|(?:исправь|измени|уточни|change|correct)(?:\s+it)?)\s+(?:на|to)\s+(.+)$",
                    clause,
                    re.I,
                )
                if match:
                    names.update(literal_names("принял " + match[1]))
            if not name_matches(value, names):
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
                r"\b(?:мг|мкг|мл|г|ме|mg|mcg|ml|g|iu|таблет(?:к[ауие]?|ок)|tablets?|кап(?:ля|ли|ель)|drops?)\b",
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
    unknown = r"(?:не\s+(?:помню|знаю)|забыл[аи]?|неизвестн\w*|forgot|unknown|(?:do not|don['’]t)\s+(?:remember|know)(?:\s+the)?)"
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


def resolve_medication_references(text, recent_events, now, *, truncated=False):
    """Resolve each pronoun against its own local or confirmed historical evidence."""
    pattern = rf"({VERB}\s+)(его|е[её]|их|it|them)\b(?!\s+easy\b)"
    if not re.search(pattern, text, re.I):
        return text
    historical = set()
    history_ambiguous = truncated
    for row in recent_events:
        if (
            row.get("kind") != "medication"
            or row.get("status") != "confirmed"
            or row.get("deleted")
        ):
            continue
        at = datetime.fromisoformat(row["start"])
        if not now - timedelta(hours=2) <= at <= now:
            continue
        name = row.get("payload", {}).get("name")
        if not name or name.casefold() in {"его", "ее", "её", "их", "it", "them"}:
            history_ambiguous = True
        else:
            historical.add(name.casefold())
    unresolved = False

    def replace(reference):
        nonlocal unresolved
        prefix = re.split(r"[;!\n]|\.(?!\d)", text[: reference.start()])[-1]
        antecedents = list(
            re.finditer(
                r"\b(?:handed|gave|passed)\s+me\s+(.+?)(?=\s+and\s+I\b|$)"
                r"|\b(?:дал[аи]?|передал[аи]?)\s+мне\s+(.+?)(?=\s+и\s+я\b|$)",
                prefix,
                re.I,
            )
        )
        local_intakes = [
            clause
            for clause in intake_sentences(
                unquote_names(calendar_dates(text[: reference.start()], now, "UTC"))
            )
            if owner_assertion(clause)
        ]
        local_doses = set()
        if local_intakes and not antecedents:
            names = set()
            ambiguous = False
            for clause in local_intakes:
                evidence = literal_names(clause)
                names.update(evidence)
                ambiguous |= not evidence or bool(re.search(QUESTION, clause, re.I))
            ambiguous |= bool(names & {"it", "them", "его", "ее", "её", "их"})
        elif antecedents:
            names = set()
            for match in antecedents:
                phrase = calendar_dates(match[1] or match[2], now, "UTC")
                names.update(literal_names("took " + phrase))
                local_doses.update(match[0] for match in re.finditer(DOSE, phrase, re.I))
            ambiguous = bool(re.search(NEGATIVE + "|" + QUESTION, prefix, re.I))
        else:
            names, ambiguous = historical, history_ambiguous
        if ambiguous or len(names) != 1 or len(local_doses) > 1:
            unresolved = True
            return reference[0]
        return (
            reference[1]
            + next(iter(names))
            + (" " + next(iter(local_doses)) if local_doses else "")
        )

    result = re.sub(pattern, replace, text, flags=re.I)
    return None if unresolved else result
