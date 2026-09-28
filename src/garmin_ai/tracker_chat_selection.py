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
    r"\b(?:coffees?|caffeine|кофе|кофеин|medications?|medicines?|лекарств\w*|таблетк\w*|"
    r"alcohol|алкогол\w*|migraines?|мигрен\w*|headaches?|головн\w*\s+бол\w*|"
    r"hydration|water|вод(?:а|ы|е|у|ой|ою)|meals?|foods?|breakfasts?|lunch(?:es)?|"
    r"dinners?|ед(?:а|ы|е|у|ой|ою)|завтрак\w*|обед\w*|ужин\w*|"
    r"illness(?:es)?|болезн\w*|naps?|sleep|сон|дрем\w*|"
    r"stressors?|stress|стресс\w*|travel|поездк\w*|moods?|настроен\w*|"
    r"activit(?:y|ies)|exercises?|workouts?|тренировк\w*|symptoms?|симптом\w*|"
    r"pain|бол(?:ь|и|ей|ями|ит|ят|ела|ело|ели|еет|еют)|energy|энерги\w*|"
    r"note|notes|заметк\w*)\b",
    re.IGNORECASE,
)
BUILTIN_QUALIFIERS = re.compile(
    r"^(?:(?:my|the|a|an|today's|yesterday's|current|"
    r"morning|afternoon|evening|nightly|daily|weekly|monthly|"
    r"мой|моя|моё|мои|мою|свою|сегодняшн\w*|вчерашн\w*|"
    r"утренн\w*|дневн\w*|вечерн\w*|ежедневн\w*)\s+){1,3}",
    re.IGNORECASE,
)


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]{3,}", value.casefold()))


def tracker_selection_cue(text: str) -> bool:
    """Recognize a local tracker request before any voice audio is transcribed."""
    cue = ENTRY_CUE.search(text.strip())
    if not cue:
        return False
    target = text.strip()[cue.end() :].strip(" \t:,.!?")
    normalized_target = target.replace("’", "'")
    qualifier = BUILTIN_QUALIFIERS.match(normalized_target)
    reserved_target = normalized_target[qualifier.end() :] if qualifier else normalized_target
    if BUILTIN_DIARY.match(reserved_target) and not re.search(
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
    normalized_target = target.replace("’", "'")
    qualifier = BUILTIN_QUALIFIERS.match(normalized_target)
    qualified_target = normalized_target[qualifier.end() :] if qualifier else normalized_target
    tracker_marker = re.match(r"^(?:tracker|трекер)\s+", qualified_target, re.IGNORECASE)
    if tracker_marker:
        target = qualified_target[tracker_marker.end() :].strip(" \t:,.!?")
    wanted = _terms(target)
    exact_label = target.casefold()
    request_tokens = set(re.findall(r"[^\W_]+", target.casefold()))
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
        if (
            len(action.label) <= 2
            and action.label.isalnum()
            and action.label.casefold() in request_tokens
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
