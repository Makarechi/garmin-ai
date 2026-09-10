"""Conservative literal evidence for newly reported incomplete medication intakes."""

import re
from datetime import timedelta


def reported_intake_times(text, now, timezone):
    from garmin_ai.diary_forms import form_time

    # Questions, hypothetical examples and quoted assertions are not intake evidence.
    if re.search(
        r"[?«»\"]|\b(?:если|бы|например|допустим|цитата|if|would|suppose|example)\b",
        text,
        re.I,
    ):
        return set()
    times = set()
    numbers = {
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
    quantity = r"(?:\d+|один|одну|два|две|три|четыре|пять|one|two|three)"
    unit = r"(?:час(?:а|ов)?|минут(?:у|ы)?|hours?|minutes?)"
    relative = rf"\b(?:(?P<n>{quantity})\s+(?P<u>{unit})|(?P<u2>{unit})\s+(?P<n2>{quantity}))\s+(?:назад|ago)\b"
    for clause in re.split(r"[,;!?]|\.(?!\d)|\b(?:но|but)\b", text, flags=re.I):
        if not re.search(r"\b(?:принял[аи]?|выпил[аи]?|принимал[аи]?|took|taken)\b", clause, re.I):
            continue
        if re.search(
            r"\b(?:не|ничего|нет|not|never|ли|(?:did|have|has|had|was|were|is|are|do|does)n['’]t)\b"
            r"|^\s*(?:did|have|has|had|was|were|is|are|do|does|when|why|what|how)\b",
            clause,
            re.I,
        ):
            continue
        for match in re.finditer(relative, clause, re.I):
            raw = (match["n"] or match["n2"]).casefold()
            count = int(raw) if raw.isdigit() else numbers[raw]
            if count > 525600:
                continue
            minutes = count * (
                60 if (match["u"] or match["u2"]).casefold().startswith(("час", "hour")) else 1
            )
            times.add(now - timedelta(minutes=minutes))
        # Match the whole offset-bearing date before its clock substring.
        pattern = r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})|\bсейчас\b|\bnow\b|\b\d{1,2}:\d{2}\b|\bв\s+\d{1,2}\b"
        for match in re.finditer(pattern, clause, re.I):
            value = match[0]
            if value.casefold() == "now":
                value = "сейчас"
            if re.fullmatch(r"в\s+\d{1,2}", value, re.I):
                value = value.split()[-1] + ":00"
            try:
                times.add(form_time(value, now, timezone))
            except (ValueError, OverflowError):
                pass
    return times
