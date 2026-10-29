"""Bounded natural-language extraction for active generated trackers.

Provider output is only a proposal. Stable IDs, permissions, source evidence and the
same generated-form application service used by deterministic clients are enforced
locally before any fact is written.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select

from garmin_ai.access import permits
from garmin_ai.accounts import owner
from garmin_ai.events import StrictModel
from garmin_ai.i18n import translate
from garmin_ai.language_tags import select_label as _label
from garmin_ai.llm import (
    Provider,
    ProviderOutputInvalid,
    ProviderRequestInvalid,
    ProviderUnavailable,
)
from garmin_ai.models import AppState, Event, EventDefinition, EventDefinitionVersion, TrackerConfig
from garmin_ai.prompts import load_prompt
from garmin_ai.tracker_forms import (
    FormSubmission,
    TrackerSetupDraft,
    action_for_event,
    available_actions,
    definition_spec,
    form_for_action,
    preview_tracker,
    submit_form,
)

SCHEMA_VERSION = "tracker.nl.v1"
PROPOSAL = re.compile(
    r"\b(?:хочу|давай|нужно|можно)\s+(?:начать\s+)?(?:отслеживать|записывать|вести)\b"
    r"|\b(?:i\s+want\s+to|let(?:'s| us)|start)\s+(?:track|log|record)\b",
    re.I,
)


class SourceEvidence(StrictModel):
    start: int = Field(ge=0, le=16000)
    end: int = Field(gt=0, le=16000)
    quote: str = Field(min_length=1, max_length=240)


class ExtractedField(StrictModel):
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    value: str | int | float | bool
    evidence: SourceEvidence
    unit: str | None = Field(default=None, max_length=32)
    unit_evidence: SourceEvidence | None = None


class TrackerExtraction(StrictModel):
    schema_version: Literal["tracker.nl.v1"]
    intent: Literal[
        "propose_tracker",
        "change_tracker",
        "create_entry",
        "update_entry",
        "clarify",
        "none",
    ]
    definition_version_id: UUID | None = None
    event_id: UUID | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    start_evidence: SourceEvidence | None = None
    end_evidence: SourceEvidence | None = None
    fields: list[ExtractedField] = Field(default_factory=list, max_length=32)
    tracker_draft: TrackerSetupDraft | None = None
    clarification: str | None = Field(default=None, max_length=500)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def complete_command(self):
        if self.intent in {"propose_tracker", "change_tracker"} and self.tracker_draft is None:
            raise ValueError("Tracker proposal requires a validated draft")
        if self.intent == "change_tracker" and self.definition_version_id is None:
            raise ValueError("Tracker change requires a definition version")
        if self.intent == "create_entry":
            if (
                self.definition_version_id is None
                or self.start is None
                or self.start_evidence is None
            ):
                raise ValueError("Entry extraction requires a version and evidenced start")
        if self.intent == "update_entry" and (
            self.event_id is None or self.definition_version_id is None
        ):
            raise ValueError("Entry update requires an event and definition version")
        return self


class NaturalLanguageRequest(StrictModel):
    text: str = Field(min_length=1, max_length=16000)
    operation_id: str = Field(min_length=1, max_length=160)
    selected_event_id: UUID | None = None
    selected_definition_version_id: UUID | None = None


INSTRUCTION = load_prompt("tracker_extract")


def _terms(value):
    return {part for part in re.findall(r"[^\W_]{3,}", value.casefold())}


def _score(text, values):
    wanted = _terms(text)
    available = _terms(" ".join(values))
    exact = len(wanted & available)
    prefix = sum(any(a[:5] == word[:5] for a in available) for word in wanted)
    return exact * 10 + prefix


def _projection(definition, version, tracker, locale):
    return {
        "definition_id": str(definition.id),
        "definition_key": definition.key,
        "definition_version_id": str(version.id),
        "version": version.version,
        "label": _label(version.labels, locale),
        "shortcut": tracker.shortcut if tracker else None,
        "topology": version.topology,
        "privacy": version.privacy,
        "schema_hash": version.schema_hash,
        "fields": [
            {
                "name": name,
                "field_id": metadata["id"],
                "label": _label(metadata["labels"], locale),
                "semantic": metadata["semantic"],
                "unit": metadata.get("unit"),
                "schema": version.schema["properties"][name],
            }
            for name, metadata in version.field_metadata.items()
        ],
    }


def _ranked_tracker_candidates(session, text, *, locale="en", limit=5):
    """Return a bounded projection and whether ranking excluded possible choices."""
    if not 1 <= limit <= 5:
        raise ValueError("Candidate limit must be between one and five")
    person = owner(session)
    rows = session.execute(
        select(EventDefinition, EventDefinitionVersion, TrackerConfig)
        .join(
            EventDefinitionVersion,
            (EventDefinitionVersion.definition_id == EventDefinition.id)
            & (EventDefinitionVersion.version == EventDefinition.current_version),
        )
        .join(TrackerConfig, TrackerConfig.definition_id == EventDefinition.id)
        .where(
            EventDefinition.owner_id == person.id,
            EventDefinition.namespace == "user",
            EventDefinition.status == "active",
        )
    ).all()
    ranked = []
    for definition, version, tracker in rows:
        values = [definition.key, tracker.shortcut or "", *version.labels.values()]
        values.extend(
            label
            for metadata in version.field_metadata.values()
            for label in metadata["labels"].values()
        )
        ranked.append((_score(text, values), definition.key, definition, version, tracker))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    candidates = [
        _projection(definition, version, tracker, locale)
        for _, _, definition, version, tracker in ranked[:limit]
    ]
    return candidates, len(ranked) > limit, ranked[0][0] if ranked else 0


def tracker_candidates(session, text, *, locale="en", limit=5):
    """Return a bounded, relevance-ordered projection of selected active trackers."""
    return _ranked_tracker_candidates(session, text, locale=locale, limit=limit)[0]


def _fallback(
    session,
    candidates,
    *,
    locale,
    granted,
    selected_action=None,
    reason="provider_unavailable",
    select_from_all=False,
):
    if not permits(granted, {"read:diary"}) and not permits(granted, {"manage:definitions"}):
        raise PermissionError("Tracker access permission required")
    forms = []
    selection_has_more = False
    candidate_ids = {UUID(row["definition_version_id"]) for row in candidates}
    if permits(granted, {"read:diary"}):
        if selected_action is not None:
            forms.append(
                form_for_action(session, selected_action.id, locale=locale).model_dump(mode="json")
            )
        else:
            resumable_versions = None
            if select_from_all:
                resumable_versions = set(
                    session.scalars(
                        select(EventDefinitionVersion.id)
                        .join(
                            EventDefinition,
                            EventDefinition.id == EventDefinitionVersion.definition_id,
                        )
                        .join(TrackerConfig, TrackerConfig.definition_id == EventDefinition.id)
                        .where(
                            EventDefinition.owner_id == owner(session).id,
                            EventDefinition.namespace == "user",
                            EventDefinition.status == "active",
                            EventDefinitionVersion.version == EventDefinition.current_version,
                        )
                    ).all()
                )
            for action in available_actions(session, locale=locale):
                if select_from_all and action.definition_version_id not in resumable_versions:
                    continue
                if select_from_all and session.info.get("channel_destination_instance_id"):
                    from garmin_ai.share_policy import track_channel_share, version_sharing_allowed

                    if not version_sharing_allowed(
                        session,
                        action.definition_version_id,
                        destination_kind="channel",
                        destination_instance_id=session.info["channel_destination_instance_id"],
                        categories={"schema"},
                    ):
                        continue
                if select_from_all or action.definition_version_id in candidate_ids:
                    if select_from_all and len(forms) >= 50:
                        selection_has_more = True
                        break
                    if select_from_all and session.info.get("channel_destination_instance_id"):
                        track_channel_share(session, action.definition_version_id, {"schema"})
                    forms.append(
                        form_for_action(session, action.id, locale=locale).model_dump(mode="json")
                    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "intent": "deterministic_form",
        "reason": reason,
        "forms": forms,
        "tracker_builder": permits(granted, {"manage:definitions"}),
        "written": False,
    }
    if select_from_all:
        result["selection_has_more"] = selection_has_more
    return result


def _verify_evidence(text, evidence):
    if (
        evidence.end <= evidence.start
        or evidence.end > len(text)
        or text[evidence.start : evidence.end] != evidence.quote
    ):
        raise ValueError("Extraction evidence does not match the source text")


def _temporal_number_spans(text, clock_values=()):
    """Find contextual clocks even when they differ from proposed entry times.

    ``clock_values`` is retained for callers, but must never limit recognition.
    """
    patterns = (
        r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})(?!\d)",
        r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?![\d:])",
    )
    spans = [match.span() for pattern in patterns for match in re.finditer(pattern, text, re.I)]
    day = (
        r"(?:(?<!\w)(?:today|yesterday|tomorrow|сегодня|вчера|завтра|now|сейчас)(?!\w)"
        r"|(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4})(?!\d))"
    )
    separator = r"\s*[,:]?\s*"
    before_clock = day + separator + r"(?:(?:T|at|from|between|в|с|между)" + separator + r")?"
    direct_starts = {match.end() for match in re.finditer(before_clock, text, re.I)}
    between_starts = {
        match.end() for match in re.finditer(r"(?<!\w)(?:between|между)\s+", text, re.I)
    }
    after_clock = re.compile(
        r"(?:" + CLOCK_OFFSET_PATTERN + r")?" + separator + r"(?:(?:on|в)\s+)?" + day,
        re.I,
    )
    am_pm = re.compile(r"\s*(?:a\.?m\.?|p\.?m\.?)(?!\w)", re.I)
    spans.extend(
        match.span()
        for match in re.finditer(
            r"(?<![\w.])(?:1[0-2]|0?[1-9])(?:[:.][0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)(?!\w)",
            text,
            re.I,
        )
    )
    clocks, _ = _clock_tokens(text, interval_hyphens=True)
    contextual = [
        start in direct_starts or bool(after_clock.match(text, end)) or bool(am_pm.match(text, end))
        for start, end, _hour, _minute in clocks
    ]
    links = []
    for previous, current in zip(clocks, clocks[1:], strict=False):
        gap = re.sub(r"^" + CLOCK_OFFSET_PATTERN, "", text[previous[1] : current[0]], flags=re.I)
        linked = bool(re.fullmatch(r"\s*(?:to|until|till|до|по|[-–—])\s*", gap, re.I))
        if not linked and re.fullmatch(r"\s*(?:and|и)\s*", gap, re.I):
            linked = previous[0] in between_starts
        links.append(linked)
    group_start = 0
    for index in range(len(clocks)):
        if index < len(links) and links[index]:
            continue
        if any(contextual[group_start : index + 1]):
            spans.extend((clock[0], clock[1]) for clock in clocks[group_start : index + 1])
        group_start = index + 1
    return spans


def _complete_number_in_source(source, start, end, attached_unit_starts=()):
    before = source[start - 1] if start else ""
    after = source[end] if end < len(source) else ""
    if before.isalnum() or (before and before in "_.,+-−﹣－٫٬"):
        return False
    if (after.isalnum() or after == "_") and (end not in attached_unit_starts or after.isdigit()):
        return False
    return not (after and after in ".,٫٬" and end + 1 < len(source) and source[end + 1].isdigit())


_BOOLEAN_LITERALS = {
    "true": True,
    "yes": True,
    "да": True,
    "есть": True,
    "false": False,
    "no": False,
    "нет": False,
}

_CONTEXT_NEGATION = re.compile(
    r"(?<!\w)(?:not|no|never|не|нет|ни|"
    r"(?:did|do|does|have|has|had|was|were|is|are|can|could|would|should|will|must)n['’]t)(?!\w)",
    re.I,
)


def _has_contextual_negation(source, start, end):
    boundaries = list(re.finditer(r"[!?]|(?<!\d)\.(?!\d)|(?<=\d)\.(?=\s|$)", source))
    before = max((match.end() for match in boundaries if match.end() <= start), default=0)
    after = min(
        (match.start() for match in boundaries if match.start() >= end), default=len(source)
    )
    return bool(_CONTEXT_NEGATION.search(source[before:start] + " " + source[end:after]))


def _folded_matches(value, quote, *, word_boundary):
    offsets = [index for index, char in enumerate(quote) for _ in char.casefold()]
    folded = quote.casefold()
    pattern = re.escape(value.casefold())
    if word_boundary:
        pattern = rf"(?<!\w){pattern}(?!\w)"
    for match in re.finditer(pattern, folded):
        start, end = offsets[match.start()], offsets[match.end() - 1] + 1
        if quote[start:end].casefold() == value.casefold():
            yield start, end


def _value_is_evidenced(
    value,
    quote,
    *,
    nominal=False,
    semantic=None,
    source_text=None,
    source_start=0,
    clock_values=(),
    attached_unit_starts=(),
):
    normalized = quote.casefold()
    if isinstance(value, bool):
        words = re.findall(r"[^\W_]+", normalized)
        affirmative = {"true", "yes", "да", "есть"}
        negative = {"false", "no", "нет"}
        if any(
            word in affirmative | negative and index and words[index - 1] in {"not", "не", "no"}
            for index, word in enumerate(words)
        ):
            return False
        positive_found = bool(affirmative.intersection(words))
        negative_found = bool(negative.intersection(words)) or "не было" in normalized
        quote_supports_value = (
            positive_found and not negative_found
            if value
            else negative_found and not positive_found
        )
        if not quote_supports_value:
            return False
        source = source_text if source_text is not None else quote
        offset = source_start if source_text is not None else 0
        terms = r"true|yes|да|есть" if value else r"не\s+было|false|no|нет"
        for match in re.finditer(rf"(?<!\w)(?:{terms})(?!\w)", quote, re.I):
            start, end = offset + match.start(), offset + match.end()
            before = source[start - 1] if start else ""
            after = source[end] if end < len(source) else ""
            if before.isalnum() or before == "_" or after.isalnum() or after == "_":
                continue
            if not _has_contextual_negation(source, start, end):
                return True
        return False
    if isinstance(value, str) and value.casefold() in _BOOLEAN_LITERALS:
        source = source_text if source_text is not None else quote
        offset = source_start if source_text is not None else 0
        expected = _BOOLEAN_LITERALS[value.casefold()]
        if not _value_is_evidenced(expected, quote, source_text=source, source_start=offset):
            return False
        return any(
            _value_is_evidenced(
                expected,
                quote[start:end],
                source_text=source,
                source_start=offset + start,
            )
            for start, end in _folded_matches(value, quote, word_boundary=True)
        )
    if isinstance(value, (int, float)):
        try:
            expected = Decimal(str(value))
        except InvalidOperation:
            return False
        source = source_text if source_text is not None else quote
        offset = source_start if source_text is not None else 0
        temporal_spans = _temporal_number_spans(source, clock_values)
        for match in re.finditer(r"[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)", quote):
            start, end = offset + match.start(), offset + match.end()
            if any(start < stop and end > begin for begin, stop in temporal_spans):
                continue
            if not _complete_number_in_source(source, start, end, attached_unit_starts):
                continue
            if Decimal(match.group().replace(",", ".")) != expected:
                continue
            if not _has_contextual_negation(source, start, end):
                return True
        return False
    if isinstance(value, str) and re.fullmatch(r"[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)", value):
        source = source_text if source_text is not None else quote
        offset = source_start if source_text is not None else 0
        temporal_spans = _temporal_number_spans(source, clock_values)
        for match in re.finditer(re.escape(value), quote):
            start, end = offset + match.start(), offset + match.end()
            if (
                _complete_number_in_source(source, start, end, attached_unit_starts)
                and not any(start < stop and end > begin for begin, stop in temporal_spans)
                and not _has_contextual_negation(source, start, end)
            ):
                return True
        return False
    if not isinstance(value, str) or not value.strip():
        return False
    source = source_text if source_text is not None else quote
    offset = source_start if source_text is not None else 0
    for start, end in _folded_matches(value, quote, word_boundary=semantic != "text"):
        start, end = offset + start, offset + end
        if semantic != "text":
            before = source[start - 1] if start else ""
            after = source[end] if end < len(source) else ""
            if before.isalnum() or before == "_" or after.isalnum() or after == "_":
                continue
        if not _has_contextual_negation(source, start, end):
            return True
    return False


def _categorical_value_is_evidenced(
    value, quote, *, source_text=None, source_start=0, clock_values=()
):
    if isinstance(value, str) and value.casefold() in _BOOLEAN_LITERALS:
        return _value_is_evidenced(value, quote, source_text=source_text, source_start=source_start)
    if re.fullmatch(r"[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)", str(value)):
        return _value_is_evidenced(
            str(value),
            quote,
            source_text=source_text,
            source_start=source_start,
            clock_values=clock_values,
        )
    expected = re.findall(r"[^\W_]+", str(value).casefold())
    observed = [
        (token, match.start(), match.end())
        for match in re.finditer(r"[^\W_]+", quote)
        for token in re.findall(r"[^\W_]+", match.group().casefold())
    ]
    source = source_text if source_text is not None else quote
    offset = source_start if source_text is not None else 0
    temporal_spans = (
        _temporal_number_spans(source, clock_values)
        if any(token.isdecimal() for token in expected)
        else ()
    )
    for index in range(len(observed) - len(expected) + 1):
        matched = observed[index : index + len(expected)]
        if not expected or [token for token, _, _ in matched] != expected:
            continue
        if _has_contextual_negation(source, offset + matched[0][1], offset + matched[-1][2]):
            continue
        if any(
            token.isdecimal()
            and any(
                offset + start < stop and offset + end > begin for begin, stop in temporal_spans
            )
            for token, start, end in matched
        ):
            continue
        if len(matched) == 1 and matched[0][0].isdecimal():
            _, start, end = matched[0]
            if not _complete_number_in_source(source, offset + start, offset + end):
                continue
        return True
    return False


UNIT_ALIASES = {
    "minutes": ("minute", "minutes", "min", "минута", "минуты", "минут", "мин"),
    "hours": ("hour", "hours", "h", "час", "часа", "часов"),
    "seconds": ("second", "seconds", "sec", "s", "секунда", "секунды", "секунд"),
}

CLOCK_OFFSET_PATTERN = (
    r"\s*(?:(?:(?:UTC|GMT)\s*)?(?P<sign>[+-])\d{2}:?\d{2}(?!\d)"
    r"|Z(?!\w)|(?:UTC|GMT)(?!\w|\s*[+-]))"
)


def _unit_spans(unit, quote, *, source_text=None, source_start=0):
    source = source_text if source_text is not None else quote
    offset = source_start if source_text is not None else 0
    aliases = UNIT_ALIASES.get(unit, (unit,))
    spans = []
    for alias in aliases:
        for start, end in _folded_matches(alias, quote, word_boundary=False):
            start, end = offset + start, offset + end
            before = source[start - 1] if start else ""
            after = source[end] if end < len(source) else ""
            if (alias[0].isalnum() or alias[0] == "_") and (
                before.isalpha() or before in {"_", "/"}
            ):
                continue
            if after.isalnum() or after in {"_", "/"}:
                continue
            spans.append((start, end))
    return spans


def _unit_is_evidenced(unit, quote, *, source_text=None, source_start=0):
    return bool(_unit_spans(unit, quote, source_text=source_text, source_start=source_start))


def _explicit_offset_matches(text, offset):
    if offset is None:
        return False
    expected = int(offset.total_seconds())
    if re.fullmatch(r"\s*(?:Z|UTC|GMT)\s*", text, re.I):
        return expected == 0
    for sign, hours, minutes in re.findall(r"([+-])(0\d|1\d|2[0-3]):?([0-5]\d)(?!\d)", text):
        observed = (int(hours) * 60 + int(minutes)) * 60
        if sign == "-":
            observed = -observed
        if observed == expected:
            return True
    return False


class DatetimeEvidence(StrictModel):
    status: Literal["supported", "ambiguous", "unverified", "contradictory"]
    date_basis: Literal["explicit_date", "relative_day", "message_time"] | None = None
    reason: str


class TemporalEvidenceError(ValueError):
    def __init__(self, field, evidence):
        super().__init__(f"{field.title()} time is not supported by its evidence")
        self.field = field
        self.evidence = evidence


class FieldEvidenceError(ValueError):
    """A proposed value could not be verified against its source."""


def _approximate_temporal_span(text, start, end):
    words = r"(?:about|around|approximately|около|примерно)"
    return bool(
        re.search(rf"(?<!\w){words}\s*(?:(?:at|в)\s*)?$", text[:start], re.I)
        or re.match(rf"\s*{words}\s*(?=$|[,;.!?])", text[end:], re.I)
    )


def _clock_calendar_evidence(quote, clocks, dates, selected, source, source_start):
    """Bind each date independently, carrying it only across an explicit interval."""
    attached = [[] for _clock in clocks]
    unattached = []
    separator = r"\s*[,:]?\s*"
    prefix_connector = separator + r"(?:(?:T|at|from|between|в|с|между)" + separator + r")?"
    optional_offset = CLOCK_OFFSET_PATTERN
    for dated in dates:
        before = [index for index, clock in enumerate(clocks) if clock[1] <= dated[0]]
        after = [index for index, clock in enumerate(clocks) if clock[0] >= dated[1]]
        left = before[-1] if before else None
        right = after[0] if after else None
        prefix_gap = quote[dated[1] : clocks[right][0]] if right is not None else None
        suffix_gap = quote[clocks[left][1] : dated[0]] if left is not None else None
        prefix = prefix_gap is not None and (
            not prefix_gap.strip() or re.fullmatch(prefix_connector, prefix_gap, re.I)
        )
        suffix = suffix_gap is not None and (
            re.fullmatch(
                r"(?:" + optional_offset + r")?" + separator + r"(?:(?:on|в)" + separator + r")?",
                suffix_gap,
                re.I,
            )
        )
        if (
            prefix
            and suffix
            and re.fullmatch(
                r"\s*[,:]\s*", re.sub(r"^" + optional_offset, "", suffix_gap, count=1, flags=re.I)
            )
        ):
            # A comma before "today at ..." separates the preceding clock's clause.
            suffix = False
        if prefix and suffix:
            return DatetimeEvidence(status="ambiguous", reason="conflicting_calendar_dates")
        if prefix:
            target = right
        elif suffix:
            target = left
        else:
            unattached.append(dated)
            continue
        attached[target].append(dated)

    groups = [[0]]
    for index in range(1, len(clocks)):
        gap = quote[clocks[index - 1][1] : clocks[index][0]]
        gap = re.sub(r"^" + optional_offset, "", gap, count=1, flags=re.I)
        between_join = re.fullmatch(r"\s*(?:and|и)\s*", gap, re.I) and re.search(
            r"(?<!\w)(?:between|между)\s*$", quote[: clocks[index - 1][0]], re.I
        )
        if between_join or re.fullmatch(r"\s*(?:to|until|till|до|по|[-–—])\s*", gap, re.I):
            groups[-1].append(index)
        else:
            groups.append([index])
    associated = set()
    basis = None
    for clock in selected:
        index = clocks.index(clock)
        evidence = attached[index]
        if not evidence:
            group = next(group for group in groups if index in group)
            evidence = [dated for member in group for dated in attached[member]]
        if not evidence:
            return DatetimeEvidence(status="unverified", reason="calendar_day_not_evidenced")
        if len({dated[2] for dated in evidence}) > 1:
            return DatetimeEvidence(status="ambiguous", reason="conflicting_calendar_dates")
        if any(dated[2] != evidence[0][2] for dated in unattached):
            return DatetimeEvidence(status="ambiguous", reason="conflicting_calendar_dates")
        for dated in evidence:
            if _approximate_temporal_span(source, source_start + dated[0], source_start + dated[1]):
                return DatetimeEvidence(status="ambiguous", reason="approximate_time")
        associated.add(evidence[0][2])
        basis = evidence[0][3]
    if len(associated) > 1:
        return DatetimeEvidence(status="ambiguous", reason="clock_has_multiple_dates")
    return associated.pop(), basis


def _clock_offset(quote, clock):
    return re.match(CLOCK_OFFSET_PATTERN, quote[clock[1] :], re.I)


def _clock_tokens(quote, *, interval_hyphens, interval_clocks=None):
    clocks = [
        (match.start(), match.end(), int(match[1]), int(match[2]))
        for match in re.finditer(r"(?<![\d:.+])([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d|[.:]\d)", quote)
    ]
    clocks.extend(
        (
            match.start(1),
            match.end(2) if match.group(2) is not None else match.end(1),
            int(match.group(1)),
            int(match.group(2) or 0),
        )
        for match in re.finditer(
            r"(?:\bat\b|\bв\b)\s+([01]?\d|2[0-3])(?:[:.]([0-5]\d))?(?!\d|[.:]\d)",
            quote,
            re.I,
        )
    )
    clocks = sorted(set(clocks))
    tokens = []
    ranges = set()
    offset_end = -1
    for index, clock in enumerate(clocks):
        if clock[0] < offset_end:
            continue
        if clock[0] and quote[clock[0] - 1] == "-" and clock[0] - 1 not in ranges:
            continue
        tokens.append(clock)
        offset = _clock_offset(quote, clock)
        offset_boundary = clock[1] + offset.end() if offset is not None else clock[1]
        next_clock = clocks[index + 1] if index + 1 < len(clocks) else None
        separator_start = clock[1]
        offset_range = False
        if offset is not None:
            following = next(
                (candidate for candidate in clocks[index + 1 :] if candidate[0] >= offset_boundary),
                None,
            )
            if following is not None and re.fullmatch(
                r"\s*-\s*", quote[offset_boundary : following[0]]
            ):
                next_clock = following
                separator_start = offset_boundary
                offset_range = True
        gap = quote[separator_start : next_clock[0]] if next_clock is not None else ""
        if (
            interval_hyphens
            and re.fullmatch(r"\s*-\s*", gap)
            and not (clock[0] and quote[clock[0] - 1] in "Tt")
            and (
                interval_clocks is None
                or (clock[2:] == interval_clocks[0] and next_clock[2:] == interval_clocks[1])
            )
        ):
            ranges.add(separator_start + gap.index("-"))
            if offset_range:
                offset_end = offset_boundary
        elif offset is not None:
            offset_end = offset_boundary
    return tokens, ranges


def _datetime_evidence(
    value,
    quote,
    timezone,
    now,
    *,
    text=None,
    quote_start=None,
    interval_hyphens=False,
    interval_clocks=None,
    non_temporal_clock_spans=(),
):
    """Prove the clock and calendar independently; an absent day stays unknown.

    The verified quote must supply both calendar and clock evidence. The full
    source can veto approximate wording even when the model clips it away.
    """
    zone = ZoneInfo(timezone)
    local = value.astimezone(zone)
    current = now.astimezone(zone)
    normalized = quote
    quoted = quote
    source = text if text is not None else quote
    source_start = quote_start if quote_start is not None else source.find(quote)
    clock_evidence, range_hyphens = _clock_tokens(
        quoted, interval_hyphens=interval_hyphens, interval_clocks=interval_clocks
    )
    matching_clocks = [
        clock
        for clock in clock_evidence
        if (clock[2], clock[3]) == (local.hour, local.minute)
        and (source_start + clock[0], source_start + clock[1]) not in non_temporal_clock_spans
    ]
    now_evidenced = re.search(r"(?<!\w)(?:сейчас|now|только что|just now)(?!\w)", quoted, re.I)
    if now_evidenced:
        # UTC subtraction measures elapsed time across both midnight and DST.
        if abs((value.astimezone(UTC) - now.astimezone(UTC)).total_seconds()) > 120:
            return DatetimeEvidence(status="contradictory", reason="message_time_mismatch")
        if not clock_evidence:
            clock_evidence = [
                (
                    now_evidenced.start(),
                    now_evidenced.end(),
                    local.hour,
                    local.minute,
                )
            ]
            matching_clocks = clock_evidence
    elif local.second or local.microsecond:
        return DatetimeEvidence(status="contradictory", reason="unsupported_clock_precision")
    if not matching_clocks:
        return DatetimeEvidence(status="unverified", reason="clock_not_evidenced")
    for clock in matching_clocks:
        if _approximate_temporal_span(source, source_start + clock[0], source_start + clock[1]):
            return DatetimeEvidence(status="ambiguous", reason="approximate_time")

    wall_time = local.replace(tzinfo=None)
    possible_offsets = {
        candidate.utcoffset()
        for fold in (0, 1)
        if (candidate := wall_time.replace(tzinfo=zone, fold=fold))
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == wall_time
    }
    if matching_clocks:
        offset_required = len(possible_offsets) > 1 and not now_evidenced
        offset_clocks = []
        for clock in matching_clocks:
            suffix = _clock_offset(quoted, clock)
            if (
                suffix is not None
                and suffix.group("sign") is not None
                and clock[1] + suffix.start("sign") in range_hyphens
            ):
                suffix = None
            if suffix is not None and _explicit_offset_matches(suffix.group(), local.utcoffset()):
                offset_clocks.append(clock)
            elif suffix is None and not offset_required:
                offset_clocks.append(clock)
        if not offset_clocks:
            return DatetimeEvidence(
                status="ambiguous" if offset_required else "contradictory",
                reason="repeated_wall_time" if offset_required else "clock_offset_mismatch",
            )
        matching_clocks = offset_clocks

    dated_evidence = []
    for match in re.finditer(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", normalized):
        try:
            dated_evidence.append(
                (
                    match.start(),
                    match.end(),
                    datetime.strptime(match.group(), "%Y-%m-%d").date(),
                    "explicit_date",
                )
            )
        except ValueError:
            return DatetimeEvidence(status="contradictory", reason="invalid_calendar_date")
    for match in re.finditer(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)", normalized):
        try:
            dated_evidence.append(
                (
                    match.start(),
                    match.end(),
                    datetime(int(match.group(3)), int(match.group(2)), int(match.group(1))).date(),
                    "explicit_date",
                )
            )
        except ValueError:
            return DatetimeEvidence(status="contradictory", reason="invalid_calendar_date")
    relative = {
        "сегодня": 0,
        "today": 0,
        "вчера": -1,
        "yesterday": -1,
        "завтра": 1,
        "tomorrow": 1,
    }
    for term, offset in relative.items():
        dated_evidence.extend(
            (match.start(), match.end(), current.date() + timedelta(days=offset), "relative_day")
            for match in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", normalized, re.I)
        )
    if dated_evidence:
        result = _clock_calendar_evidence(
            quoted, clock_evidence, sorted(dated_evidence), matching_clocks, source, source_start
        )
        if isinstance(result, DatetimeEvidence):
            return result
        day, basis = result
        if day == local.date():
            return DatetimeEvidence(
                status="supported", date_basis=basis, reason="calendar_and_clock"
            )
        return DatetimeEvidence(status="contradictory", reason="calendar_date_mismatch")
    if now_evidenced:
        return DatetimeEvidence(
            status="supported", date_basis="message_time", reason="message_time"
        )
    return DatetimeEvidence(status="unverified", reason="calendar_day_not_evidenced")


def _datetime_is_evidenced(value, quote, timezone, now):
    return _datetime_evidence(value, quote, timezone, now).status == "supported"


def _dotted_quantity_spans(text, fields, metadata, clock_values):
    """Separate a plainly unit-qualified decimal from clock-like text."""
    temporal_spans = _temporal_number_spans(text, clock_values)
    result = set()
    for field in fields:
        contract = metadata.get(field.field_id, {})
        number = field.evidence
        unit = field.unit_evidence
        expected_unit = contract.get("unit")
        unit_spans = (
            _unit_spans(expected_unit, unit.quote, source_text=text, source_start=unit.start)
            if expected_unit
            and unit is not None
            and 0 <= unit.start < unit.end <= len(text)
            and text[unit.start : unit.end] == unit.quote
            else []
        )
        if (
            contract.get("semantic") != "quantity"
            or not expected_unit
            or field.unit != expected_unit
            or unit is None
            or not isinstance(field.value, (int, float))
            or isinstance(field.value, bool)
            or not re.fullmatch(r"[-+]?\d+\.\d+", number.quote)
            or number.end > len(text)
            or unit.end > len(text)
            or text[number.start : number.end] != number.quote
            or text[unit.start : unit.end] != unit.quote
            or unit.start < number.end
            or text[number.end : unit.start].strip()
            or not unit_spans
            or not _complete_number_in_source(
                text, number.start, number.end, {start for start, _ in unit_spans}
            )
            or any(number.start < stop and number.end > begin for begin, stop in temporal_spans)
        ):
            continue
        try:
            if Decimal(number.quote) == Decimal(str(field.value)):
                result.add((number.start, number.end))
        except InvalidOperation:
            continue
    return result


def _candidate(candidates, version_id):
    return next(
        (row for row in candidates if row["definition_version_id"] == str(version_id)),
        None,
    )


def _validated_submission(text, extraction, candidate, form, timezone, now):
    start = extraction.start or form.initial_start
    end = extraction.end if extraction.end is not None else form.initial_end
    evidence_timezone = form.initial_timezone or timezone
    interval_clocks = None
    if (
        form.topology == "bounded_interval"
        and extraction.start is not None
        and extraction.end is not None
    ):
        zone = ZoneInfo(evidence_timezone)
        interval_clocks = tuple(
            (value.astimezone(zone).hour, value.astimezone(zone).minute)
            for value in (extraction.start, extraction.end)
        )
    if start is None:
        raise ValueError("Entry time is unavailable")
    metadata = {field["field_id"]: field for field in candidate["fields"]}
    names = {field["field_id"]: field["name"] for field in candidate["fields"]}
    zone = ZoneInfo(evidence_timezone)
    clock_values = {
        (local.hour, local.minute)
        for value in (start, end)
        if value is not None
        for local in (value.astimezone(zone),)
    }
    non_temporal_clock_spans = _dotted_quantity_spans(
        text, extraction.fields, metadata, clock_values
    )
    if extraction.start is not None:
        if extraction.start_evidence is None:
            raise ValueError("Changed start requires evidence")
        _verify_evidence(text, extraction.start_evidence)
        result = _datetime_evidence(
            extraction.start,
            extraction.start_evidence.quote,
            evidence_timezone,
            now,
            text=text,
            quote_start=extraction.start_evidence.start,
            interval_hyphens=interval_clocks is not None,
            interval_clocks=interval_clocks,
            non_temporal_clock_spans=non_temporal_clock_spans,
        )
        if result.status != "supported":
            raise TemporalEvidenceError("start", result)
    if form.topology == "bounded_interval" and end is None:
        raise ValueError("Bounded interval requires an end")
    if extraction.end is not None:
        if extraction.end_evidence is None:
            raise ValueError("Changed end requires evidence")
        _verify_evidence(text, extraction.end_evidence)
        result = _datetime_evidence(
            end,
            extraction.end_evidence.quote,
            evidence_timezone,
            now,
            text=text,
            quote_start=extraction.end_evidence.start,
            interval_hyphens=interval_clocks is not None,
            interval_clocks=interval_clocks,
            non_temporal_clock_spans=non_temporal_clock_spans,
        )
        if result.status != "supported":
            raise TemporalEvidenceError("end", result)
    values = dict(form.initial_values)
    units = dict(form.initial_units)
    seen = set()
    evidence_refs = []
    for field in extraction.fields:
        if field.field_id in seen or field.field_id not in metadata:
            raise ValueError("Extracted field is duplicated or outside the selected schema")
        seen.add(field.field_id)
        contract = metadata[field.field_id]
        _verify_evidence(text, field.evidence)
        expected_unit = contract.get("unit")
        attached_unit_starts = set()
        if contract["semantic"] == "quantity":
            if field.unit != expected_unit or field.unit_evidence is None:
                raise ValueError("Quantity requires its configured unit and evidence")
            _verify_evidence(text, field.unit_evidence)
            unit_spans = _unit_spans(
                expected_unit,
                field.unit_evidence.quote,
                source_text=text,
                source_start=field.unit_evidence.start,
            )
            if not unit_spans:
                raise FieldEvidenceError("Quantity unit is not supported by its evidence")
            attached_unit_starts = {start for start, _ in unit_spans}
        elif field.unit not in {None, expected_unit}:
            raise ValueError("Extracted unit does not match the selected field")
        value_is_evidenced = (
            _categorical_value_is_evidenced(
                field.value,
                field.evidence.quote,
                source_text=text,
                source_start=field.evidence.start,
                clock_values=clock_values,
            )
            if contract["semantic"] == "nominal"
            else _value_is_evidenced(
                field.value,
                field.evidence.quote,
                source_text=text,
                source_start=field.evidence.start,
                clock_values=clock_values,
                attached_unit_starts=attached_unit_starts,
            )
        )
        if not value_is_evidenced:
            raise FieldEvidenceError("Extracted value is not supported by its evidence")
        values[names[field.field_id]] = field.value
        if expected_unit:
            units[names[field.field_id]] = expected_unit
        evidence_refs.append(
            {
                "schema_version": SCHEMA_VERSION,
                "field_id": field.field_id,
                "start": field.evidence.start,
                "end": field.evidence.end,
            }
        )
    return (
        FormSubmission(
            action_id=form.id,
            schema_hash=form.schema_hash,
            start=start,
            end=end,
            timezone=form.initial_timezone or timezone,
            values=values,
            units=units,
        ),
        evidence_refs,
    )


def process_tracker_text(
    session,
    provider: Provider | None,
    request,
    *,
    granted,
    actor,
    now=None,
    timezone="UTC",
    locale="en",
    source: Literal["manual", "telegram_text", "telegram_voice", "mcp"] = "manual",
):
    """Interpret and safely apply one natural-language tracker command."""
    request = NaturalLanguageRequest.model_validate(request)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError("Unknown timezone") from None
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Message time must include a timezone")
    if not permits(granted, {"read:diary"}) and not permits(granted, {"manage:definitions"}):
        raise PermissionError("Tracker access permission required")
    if request.selected_event_id is not None and not permits(granted, {"read:diary"}):
        raise PermissionError("Diary read permission required")
    operation_key = (
        "nl-operation:" + sha256(f"{actor}\0{request.operation_id}".encode()).hexdigest()
    )
    request_hash = sha256(request.model_dump_json(exclude_none=False).encode()).hexdigest()
    compatible_request_hashes = {request_hash}
    if request.selected_definition_version_id is None:
        compatible_request_hashes.add(
            sha256(
                request.model_dump_json(
                    exclude={"selected_definition_version_id"}, exclude_none=False
                ).encode()
            ).hexdigest()
        )
    session.execute(select(func.pg_advisory_xact_lock(72104623, func.hashtext(operation_key))))
    receipt = session.get(AppState, operation_key, populate_existing=True)
    if receipt is not None:
        if receipt.value.get("request_hash") not in compatible_request_hashes:
            raise ValueError("Operation ID was already used for a different request")
        if receipt.value.get("result", {}).get("written") and not permits(
            granted, {"read:diary", "write:diary"}
        ):
            raise PermissionError("Diary write permission required")
        return receipt.value["result"]
    candidates, has_more_candidates, best_score = _ranked_tracker_candidates(
        session, request.text, locale=locale
    )
    if request.selected_definition_version_id is not None:
        version = session.get(EventDefinitionVersion, request.selected_definition_version_id)
        definition = session.get(EventDefinition, version.definition_id) if version else None
        tracker = (
            session.scalar(
                select(TrackerConfig).where(TrackerConfig.definition_id == definition.id)
            )
            if definition is not None
            else None
        )
        if (
            definition is None
            or tracker is None
            or definition.owner_id != owner(session).id
            or definition.namespace != "user"
            or definition.status != "active"
            or definition.current_version != version.version
        ):
            raise LookupError("Selected tracker form is no longer active")
        candidates = [_projection(definition, version, tracker, locale)]
    selected = None
    selected_action = None
    selected_version = None
    if request.selected_event_id is not None:
        selected_action = action_for_event(session, request.selected_event_id, locale=locale)
        event = session.get(Event, request.selected_event_id)
        if event is None or event.deleted or event.definition_version_id is None:
            raise LookupError("Selected tracker entry not found")
        version = session.get(EventDefinitionVersion, event.definition_version_id)
        selected_version = version
        definition = session.get(EventDefinition, version.definition_id) if version else None
        tracker = (
            session.scalar(
                select(TrackerConfig).where(TrackerConfig.definition_id == definition.id)
            )
            if definition is not None
            else None
        )
        if (
            definition is None
            or definition.owner_id != owner(session).id
            or definition.namespace != "user"
        ):
            raise LookupError("Selected tracker entry not found")
        projection = _projection(definition, version, tracker, locale)
        if all(
            row["definition_version_id"] != projection["definition_version_id"]
            for row in candidates
        ):
            candidates = [*candidates[:4], projection]
        selected = {
            "id": str(event.id),
            "revision": event.revision,
            "definition_version_id": str(event.definition_version_id),
        }
    # A retry may happen after midnight, a restart, or a profile-zone change.
    # Keep the first trusted interpretation context, including clarification and
    # provider-fallback attempts, without caching an unsuccessful model answer.
    context_key = operation_key.replace("nl-operation:", "nl-context:", 1)
    context = session.get(AppState, context_key, populate_existing=True)
    if context is not None:
        if context.value.get("request_hash") not in compatible_request_hashes:
            raise ValueError("Operation ID was already used for a different request")
        now = datetime.fromisoformat(context.value["message_at"])
        timezone = context.value["timezone"]
    else:
        session.add(
            AppState(
                key=context_key,
                value={
                    "request_hash": request_hash,
                    "message_at": now.isoformat(),
                    "timezone": timezone,
                },
            )
        )
        session.flush()
    if (
        has_more_candidates
        and best_score < 10
        and request.selected_event_id is None
        and request.selected_definition_version_id is None
        and not PROPOSAL.search(request.text)
    ):
        return _fallback(
            session,
            candidates,
            locale=locale,
            granted=granted,
            reason="tracker_selection_required",
            select_from_all=True,
        )
    expand_offline_selection = (
        has_more_candidates
        and request.selected_event_id is None
        and request.selected_definition_version_id is None
    )
    if provider is None:
        return _fallback(
            session,
            candidates,
            locale=locale,
            granted=granted,
            selected_action=selected_action,
            select_from_all=expand_offline_selection,
        )
    from garmin_ai.share_policy import sharing_allowed, version_sharing_allowed

    provider_instance_id = getattr(provider, "instance_id", "model:gemini:primary")
    session.info["model_provider_instance_id"] = provider_instance_id
    if selected_version is not None and not version_sharing_allowed(
        session,
        selected_version.id,
        destination_kind="model",
        destination_instance_id=provider_instance_id,
        categories={"schema", "facts"}
        | ({"original_text"} if selected_version.privacy == "sensitive" else set()),
    ):
        return _fallback(
            session,
            candidates,
            locale=locale,
            granted=granted,
            selected_action=selected_action,
            reason="sensitive_tracker_consent_required",
        )
    shareable = [
        candidate
        for candidate in candidates
        if sharing_allowed(
            session,
            UUID(candidate["definition_id"]),
            destination_kind="model",
            destination_instance_id=provider_instance_id,
            categories={"schema", "facts"}
            | ({"original_text"} if candidate["privacy"] == "sensitive" else set()),
        )
    ]
    if selected is not None and not any(
        candidate["definition_version_id"] == selected["definition_version_id"]
        for candidate in shareable
    ):
        return _fallback(
            session,
            candidates,
            locale=locale,
            granted=granted,
            reason="sensitive_tracker_consent_required",
        )
    if candidates and candidates[0] not in shareable:
        return _fallback(
            session,
            candidates,
            locale=locale,
            granted=granted,
            reason="sensitive_tracker_consent_required",
        )
    if candidates:
        candidates = shareable
    prompt = json.dumps(
        {
            "now": now.astimezone(ZoneInfo(timezone)).isoformat(),
            "timezone": timezone,
            "text": request.text,
            "candidate_trackers": candidates,
            "selected_event": selected,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        from garmin_ai.llm import structured_with_categories

        extraction = structured_with_categories(
            provider, INSTRUCTION, prompt, TrackerExtraction, {"diary"}
        )
    except (ProviderUnavailable, ProviderOutputInvalid, ProviderRequestInvalid):
        return _fallback(
            session,
            candidates,
            locale=locale,
            granted=granted,
            selected_action=selected_action,
            select_from_all=expand_offline_selection,
        )
    extraction = TrackerExtraction.model_validate(extraction)
    if extraction.confidence < 0.85:
        return {
            "schema_version": SCHEMA_VERSION,
            "intent": "clarify",
            "clarification": extraction.clarification or "Please clarify the tracker and details.",
            "written": False,
        }
    if PROPOSAL.search(request.text) and extraction.intent in {"create_entry", "update_entry"}:
        return {
            "schema_version": SCHEMA_VERSION,
            "intent": "clarify",
            "clarification": "This sounds like a tracker setup request, not a completed entry.",
            "written": False,
        }
    if extraction.intent == "propose_tracker":
        if not permits(granted, {"manage:definitions"}):
            raise PermissionError("Definition management permission required")
        preview = preview_tracker(session, extraction.tracker_draft)
        return {
            "schema_version": SCHEMA_VERSION,
            "intent": "tracker_proposal",
            "preview": preview,
            "written": False,
        }
    if extraction.intent == "change_tracker":
        if not permits(granted, {"manage:definitions"}):
            raise PermissionError("Definition management permission required")
        candidate = _candidate(candidates, extraction.definition_version_id)
        if candidate is None or extraction.tracker_draft.key != candidate[
            "definition_key"
        ].removeprefix("user."):
            raise ValueError("Definition change is outside the selected tracker")
        return {
            "schema_version": SCHEMA_VERSION,
            "intent": "definition_change_proposal",
            "definition_version_id": candidate["definition_version_id"],
            "definition": definition_spec(extraction.tracker_draft).model_dump(
                mode="json", by_alias=True
            ),
            "written": False,
        }
    if extraction.intent in {"clarify", "none"}:
        return {
            "schema_version": SCHEMA_VERSION,
            "intent": extraction.intent,
            "clarification": extraction.clarification,
            "written": False,
        }
    if not permits(granted, {"read:diary", "write:diary"}):
        raise PermissionError("Diary write permission required")
    candidate = _candidate(candidates, extraction.definition_version_id)
    if candidate is None:
        raise ValueError("Provider selected a tracker outside the bounded context")
    if extraction.intent == "update_entry":
        if request.selected_event_id is None or extraction.event_id != request.selected_event_id:
            raise ValueError("Entry update must use the explicitly selected event")
        action = action_for_event(session, request.selected_event_id, locale=locale)
        if action.definition_version_id != extraction.definition_version_id:
            raise ValueError("Selected event does not use the extracted definition version")
    else:
        action = next(
            (
                action
                for action in available_actions(session, locale=locale)
                if action.definition_version_id == extraction.definition_version_id
            ),
            None,
        )
        if action is None:
            raise ValueError("Selected tracker does not allow new entries")
    form = form_for_action(session, action.id, locale=locale)
    try:
        submission, evidence_refs = _validated_submission(
            request.text, extraction, candidate, form, timezone, now
        )
    except TemporalEvidenceError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "intent": "clarify",
            "clarification": translate(
                {
                    "repeated_wall_time": "agent.ambiguous_time",
                    "clock_offset_mismatch": "agent.timezone_mismatch",
                }.get(exc.evidence.reason, "tracker.time_clarification"),
                locale,
            ),
            "temporal_evidence": {"field": exc.field, **exc.evidence.model_dump()},
            "written": False,
        }
    except FieldEvidenceError:
        return {
            "schema_version": SCHEMA_VERSION,
            "intent": "clarify",
            "clarification": translate("tracker.value_clarification", locale),
            "written": False,
        }
    submission_evidence_refs = evidence_refs
    if extraction.intent == "update_entry":
        submission_evidence_refs = [
            ref
            for ref in form.initial_evidence_refs
            if not (isinstance(ref, dict) and "field_id" in ref)
        ] + evidence_refs
    event = submit_form(
        session,
        action.id,
        submission,
        actor=actor,
        source=source,
        idempotency_key=(
            f"nl:{request.operation_id}" if extraction.intent == "create_entry" else None
        ),
        original_text=request.text,
        evidence_refs=submission_evidence_refs,
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "intent": extraction.intent,
        "event_id": str(event.id),
        "revision": event.revision,
        "definition_version_id": str(event.definition_version_id),
        "evidence_refs": evidence_refs,
        "written": True,
    }
    session.add(
        AppState(
            key=operation_key,
            value={"request_hash": request_hash, "result": result},
        )
    )
    session.flush()
    return result
