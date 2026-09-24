"""Bounded, local selection of permitted trackers from ordinary chat text."""

import re

from garmin_ai.share_policy import version_sharing_allowed
from garmin_ai.tracker_forms import available_actions


def _terms(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]{3,}", value.casefold())) - {
        "записать",
        "отметить",
        "добавить",
        "record",
        "track",
        "log",
    }


def select_tracker_actions(session, text: str, *, locale: str, destination: str):
    """Return up to five matching create actions, without disclosing hidden schemas."""
    wanted = _terms(text)
    if not wanted:
        return []
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
        overlap = sum(
            any(word == name or word[:5] == name[:5] for name in names) for word in wanted
        )
        if overlap:
            matches.append((overlap, action.definition_key, action))
    if not matches:
        return []
    matches.sort(key=lambda item: (-item[0], item[1]))
    best = matches[0][0]
    return [action for score, _, action in matches if score == best][:5]
