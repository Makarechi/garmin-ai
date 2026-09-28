"""Bounded, local selection of permitted trackers from ordinary chat text."""

import re

from garmin_ai.share_policy import version_sharing_allowed
from garmin_ai.tracker_forms import available_actions

ENTRY_CUE = re.compile(
    r"^(?:(?:я|I)\s+)?(?:записал[аи]?|запиши(?:те)?|записать|добавить|отметить|отметил[аи]?|"
    r"внёс|внес|внесла|внести|add|added|log|logged|record|recorded|track|tracked)\b",
    re.IGNORECASE,
)
BUILTIN_DIARY = re.compile(
    r"\b(?:coffee|caffeine|кофе|кофеин|medication|medicine|лекарств\w*|таблетк\w*|"
    r"alcohol|алкогол\w*|migraine|мигрен\w*|headache|головн\w*\s+бол\w*|"
    r"hydration|water|вод\w*|meal|food|breakfast|lunch|dinner|ед\w*|завтрак\w*|"
    r"обед\w*|ужин\w*|illness|болезн\w*|nap|sleep|сон|дрем\w*|"
    r"stressor|stress|стресс\w*|travel|поездк\w*|mood|настроен\w*|"
    r"activity|exercise|workout|тренировк\w*|symptom|симптом\w*|"
    r"pain|бол(?:ь|и|ей|ями|ит|ят|ела|ело|ели|еет|еют)|energy|энерги\w*|"
    r"note|notes|заметк\w*)\b",
    re.IGNORECASE,
)
SHORT_FILLER = {
    "a",
    "an",
    "as",
    "at",
    "be",
    "by",
    "do",
    "i",
    "if",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "or",
    "so",
    "to",
    "up",
    "we",
    "я",
    "в",
    "и",
    "на",
    "не",
    "по",
    "за",
    "от",
    "из",
    "до",
}


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]{3,}", value.casefold()))


def tracker_selection_cue(text: str) -> bool:
    """Recognize a local tracker request before any voice audio is transcribed."""
    cue = ENTRY_CUE.search(text.strip())
    if not cue:
        return False
    target = text.strip()[cue.end() :].strip(" \t:,.!?")
    normalized_target = target.replace("’", "'")
    if BUILTIN_DIARY.search(normalized_target) and not re.search(
        r"\b(?:tracker|трекер)\b", target, re.I
    ):
        return False
    return bool(target)


def select_tracker_actions(session, text: str, *, locale: str, destination: str):
    """Return up to five matching create actions, without disclosing hidden schemas."""
    if not tracker_selection_cue(text):
        return []
    cue = ENTRY_CUE.search(text.strip())
    target = text.strip()[cue.end() :].strip(" \t:,.!?")
    raw_target = target
    short_target = re.sub(
        r"^(?:(?:my|the|a|an|мой|моя|моё|мои)\s+)*(?:(?:tracker|трекер)\s+)?",
        "",
        raw_target,
        flags=re.IGNORECASE,
    )
    short_word = re.match(r"[^\W_]+", short_target)
    explicit_marker = bool(
        re.match(
            r"^(?:(?:my|the|a|an|мой|моя|моё|мои)\s+)*(?:tracker|трекер)\s+\S",
            raw_target,
            re.IGNORECASE,
        )
    )
    if explicit_marker:
        target = short_target.strip(" \t:,.!?")
    wanted = _terms(target)
    exact_label = target.casefold()
    matches = []
    for action in available_actions(session, locale=locale):
        if not version_sharing_allowed(
            session,
            action.definition_version_id,
            destination_kind="channel",
            destination_instance_id=destination,
            categories={"schema"},
        ):
            continue
        names = _terms(action.label)
        overlap = sum(word in names for word in wanted)
        if exact_label and exact_label == action.label.casefold():
            overlap = len(wanted) + 1
        if explicit_marker and short_target.casefold() == action.label.casefold():
            overlap = len(wanted) + 1
        if (
            len(action.label) <= 2
            and action.label.isalnum()
            and short_word
            and short_word.group().casefold() == action.label.casefold()
            and (action.label.casefold() not in SHORT_FILLER or explicit_marker)
        ):
            overlap = max(overlap, 1)
        short_label_tokens = re.findall(r"[^\W_]+", action.label.casefold())
        if (
            len(short_label_tokens) > 1
            and all(len(token) <= 2 for token in short_label_tokens)
            and re.search(
                r"(?<!\w)" + r"\W+".join(map(re.escape, short_label_tokens)) + r"(?!\w)",
                exact_label,
            )
        ):
            overlap = max(overlap, len(short_label_tokens))
        if len(names) > 1 and overlap == 1 and len(wanted) > 1:
            continue
        if overlap:
            matches.append((overlap, action.definition_key, action))
    if not matches:
        return []
    matches.sort(key=lambda item: (-item[0], item[1]))
    best = matches[0][0]
    return [action for score, _, action in matches if score == best][:5]
