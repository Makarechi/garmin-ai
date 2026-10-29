import json
import re
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, Field, PrivateAttr, model_validator
from sqlalchemy import func, or_, select

from garmin_ai.claims import (
    NumericClaim,
    TimestampClaim,
    verified_numbers,
    verified_timestamps,
)
from garmin_ai.config import Settings
from garmin_ai.event_batches import DraftLink, create_batch, validate_links
from garmin_ai.events import (
    OPEN_EPISODE_KINDS,
    EventInput,
    StrictModel,
    create_event,
    event_query_allowed,
    lock_writes,
    serialize,
    undo_last,
    update_event,
)
from garmin_ai.i18n import translate
from garmin_ai.llm import (
    Provider,
    ProviderOutputInvalid,
    ProviderRequestInvalid,
    ProviderUnavailable,
    compact,
)
from garmin_ai.models import AppState, Event, PendingQuestion
from garmin_ai.normalize import upsert
from garmin_ai.pending_state import pending_key
from garmin_ai.prompts import load_prompt
from garmin_ai.tools import TOOLS, ReplayUnavailable, call_tool


def normalized_response_language(value):
    primary, *rest = value.split("-")
    return "-".join([primary.lower(), *rest])


ResponseLanguage = Annotated[
    str,
    Field(min_length=2, max_length=255, pattern=r"^[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*$"),
    AfterValidator(normalized_response_language),
]


class Interpretation(StrictModel):
    _target_revision: int | None = PrivateAttr(default=None)
    _question_ids: set[UUID] | None = PrivateAttr(default=None)
    _dismiss_refinement: bool = PrivateAttr(default=False)
    intent: Literal[
        "log", "update", "close", "undo", "question", "clarify", "safety", "acknowledge"
    ]
    events: list[EventInput] = Field(default_factory=list, max_length=10)
    draft_links: list[DraftLink] = Field(default_factory=list, max_length=10)
    target_event_id: UUID | None = None
    target_question_id: UUID | None = None
    question_context_ids: list[UUID] = Field(
        default_factory=list,
        max_length=20,
        description="Only offered pending questions whose context is needed to interpret this message; empty for independent messages.",
    )
    clarification: str | None = None
    confidence: float = Field(ge=0, le=1)
    response_language: ResponseLanguage | None = None
    changed_fields: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def consistent(self):
        if self.draft_links:
            if self.intent != "log":
                raise ValueError("Draft links only apply to new event batches")
            validate_links(self.events, self.draft_links)
        if self.intent in {"log", "update", "close"} and not self.events:
            raise ValueError("Mutation requires validated event data")
        if self.intent in {"update", "close"} and (not self.target_event_id):
            raise ValueError("Update must identify one target event")
        return self


class SafetyScreen(StrictModel):
    urgent: bool
    response_language: ResponseLanguage | None = None


OVERSIZED_NOTICE = translate("agent.oversized", "ru")


def oversized_notice(locale):
    return translate("agent.oversized", locale)


def screen_oversized(provider, text, before_model, *, locale="ru"):
    instruction = load_prompt("safety_fragment")
    language = locale
    # Bound provider work and overlap boundaries. Preserve the language even
    # when the unprocessed tail or a provider outage requires trusted guidance.
    for start in range(0, min(len(text), 48000), 12000):
        if before_model:
            before_model()
        try:
            result = provider.structured(instruction, text[start : start + 12256], SafetyScreen)
        except (ProviderUnavailable, ProviderOutputInvalid, ProviderRequestInvalid):
            return Interpretation(
                intent="safety",
                confidence=0,
                response_language=language,
                clarification=oversized_notice(language),
            )
        language = result.response_language or language
        if result.urgent:
            return Interpretation(intent="safety", confidence=1, response_language=language)
    return Interpretation(
        intent="safety" if len(text) > 48000 else "clarify",
        confidence=0,
        response_language=language,
        clarification=oversized_notice(language),
    )


def screen_reply_safety(provider, text, before_model=None, *, locale="ru"):
    if len(text) > 16000:
        return screen_oversized(provider, text, before_model, locale=locale)
    if before_model:
        before_model()
    screened = provider.structured(
        load_prompt("safety_reply"),
        text,
        SafetyScreen,
    )
    return Interpretation(
        intent="safety" if screened.urgent else "clarify",
        confidence=1,
        response_language=screened.response_language or locale,
    )


class ReadCall(StrictModel):
    name: str
    arguments_json: str


class AgentStep(StrictModel):
    response_language: ResponseLanguage | None = None
    calls: list[ReadCall] = Field(default_factory=list, max_length=4)
    answer: str | None = None
    urgent_safety: bool = False
    include_current_state_freshness: bool = False
    evidence_ids: list[int] = Field(default_factory=list, max_length=20)
    numeric_claims: list[NumericClaim] = Field(default_factory=list, max_length=20)
    timestamp_claims: list[TimestampClaim] = Field(default_factory=list, max_length=20)


EXTRACT_INSTRUCTION = load_prompt("diary_extract")


def pending_clarification(session, now, *, use_message_time=False):
    pending = session.get(AppState, pending_key(session), populate_existing=True)
    if not pending:
        return None
    destination = session.info.get("channel_destination_instance_id")
    if destination and pending.value.get("channel_instance_id", "telegram:primary") != destination:
        return None
    if not pending.value.get("explicit_selector") and not use_message_time:
        now = session.info.get("conversation_now", now)
    try:
        created = datetime.fromisoformat(
            pending.value.get("preset_selected_at", pending.value["created_at"])
        )
        if created.tzinfo is None or not timedelta(0) <= now - created <= timedelta(hours=2):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return pending


def any_pending_clarification(session, now):
    """Check active forms across the owner's channel instances before notifying."""
    destination = session.info.get("channel_destination_instance_id")
    keys = session.scalars(
        select(AppState.key).where(
            or_(
                AppState.key == "conversation:pending",
                AppState.key.startswith("conversation:pending:"),
            )
        )
    ).all()
    try:
        for key in keys:
            session.info["channel_destination_instance_id"] = (
                key.removeprefix("conversation:pending:")
                if key != "conversation:pending"
                else "telegram:primary"
            )
            if pending_clarification(session, now):
                return True
        return False
    finally:
        if destination is None:
            session.info.pop("channel_destination_instance_id", None)
        else:
            session.info["channel_destination_instance_id"] = destination


def queryable_event(session, identity):
    return session.scalar(select(Event).where(Event.id == identity, event_query_allowed()))


def context_for(session, now, *, maintain=True):
    from garmin_ai.conversation import conversation_context
    from garmin_ai.proactive import reconcile_answers
    from garmin_ai.scenario_packs import llm_allows_event, llm_allows_question, llm_event_filter

    if maintain:
        reconcile_answers(session, now)

    def queryable_context_event(identity):
        row = queryable_event(session, identity)
        return row if row is not None and llm_allows_event(session, row, track=False) else None

    recent = session.scalars(
        select(Event)
        .where(
            Event.deleted.is_(False),
            Event.start >= now - timedelta(days=14),
            Event.start <= now,
            event_query_allowed(),
            llm_event_filter(session),
        )
        .order_by(Event.start.desc())
        .limit(13)
    ).all()
    recent = [row for row in recent if llm_allows_event(session, row, track=False)]
    truncated = len(recent) > 12
    recent = recent[:12]
    identities = {row.id for row in recent}
    open_rows = session.scalars(
        select(Event)
        .where(
            Event.topology == "open_interval",
            Event.deleted.is_(False),
            event_query_allowed(),
            llm_event_filter(session),
            or_(Event.end.is_(None), Event.end > now),
            Event.start <= now,
        )
        .order_by(Event.start.desc())
        .limit(21)
    ).all()
    truncated = truncated or len(open_rows) > 20
    for row in open_rows[:20]:
        if not llm_allows_event(session, row, track=False):
            continue
        if row.id not in identities:
            recent.append(row)
            identities.add(row.id)
    pending = pending_clarification(session, now)
    questions = session.scalars(
        select(PendingQuestion)
        .where(
            PendingQuestion.status.in_(["sent", "uncertain", "acknowledged"]),
            PendingQuestion.expires_at > now,
            PendingQuestion.sent_at <= now,
        )
        .order_by(PendingQuestion.sent_at.desc())
    ).all()
    questions = [question for question in questions if llm_allows_question(session, question.kind)]
    questions = [
        question
        for question in questions
        if question.event_id is None or queryable_context_event(question.event_id) is not None
    ]
    identities = {r.id for r in recent}
    for question in questions:
        if question.event_id and question.event_id not in identities:
            target = queryable_context_event(question.event_id)
            if target and not target.deleted and target.start <= now:
                recent.append(target)
                identities.add(target.id)
    if pending:
        known_ids = {r.id for r in recent}
        for identity in pending.value.get("event_ids", []):
            target = queryable_context_event(UUID(identity))
            if target and not target.deleted and target.id not in known_ids:
                recent.append(target)
                known_ids.add(target.id)
    pending_context = pending.value if pending else None
    pending_pack = pending_context.get("pack") if pending_context else None
    if pending_context and pending_pack is None:
        pending_pack = {
            "coffee": "caffeine",
            "coffee_preset": "caffeine",
            "migraine": "migraine",
            "end": "migraine",
            "medication": "migraine",
            "alcohol": "general_diary",
            "note": "general_diary",
        }.get(pending_context.get("button"))
    if pending_context and pending_pack is not None:
        from garmin_ai.scenario_packs import pack_enabled

        if not pack_enabled(session, pending_pack, "llm"):
            pending_context = None
    if pending_context and any(
        queryable_context_event(UUID(identity)) is None
        for identity in pending_context.get("event_ids", [])
    ):
        pending_context = None
    analytic_turns = conversation_context(session, now, maintain=bool(maintain))["turns"]
    for row in recent:
        llm_allows_event(session, row)
    return {
        "recent_analysis_question": {
            "question": analytic_turns[-1]["question"][:400],
            "asked_at": analytic_turns[-1]["asked_at"],
        }
        if analytic_turns
        else None,
        "recent_events": [serialize(r) for r in recent],
        "history_truncated": truncated,
        "pending_clarification": pending_context,
        "recent_questions": [serialize(q) for q in questions],
    }


def interpret(
    session,
    provider: Provider,
    text: str,
    settings: Settings,
    now: datetime,
    source="telegram_text",
    before_model=None,
    budget=None,
):
    from garmin_ai.accounts import effective_owner_settings

    settings = effective_owner_settings(session, settings)
    language = settings.locale
    session.info["model_provider_instance_id"] = getattr(
        provider,
        "instance_id",
        session.info.get("model_provider_instance_id", "model:gemini:primary"),
    )
    if len(text) > 16000:
        return screen_oversized(provider, text, before_model, locale=settings.locale)
    context = context_for(session, now, maintain=False)
    identities = list(
        dict.fromkeys(
            UUID(value)
            for value in re.findall(
                r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])",
                text,
            )
        )
    )
    if len(identities) > 20:
        return Interpretation(
            intent="clarify",
            confidence=0,
            clarification=translate("agent.too_many_records", language),
        )
    from garmin_ai.scenario_packs import llm_allows_event

    explicit = [
        serialize(row)
        for identity in identities
        if (row := queryable_event(session, identity))
        and not row.deleted
        and llm_allows_event(session, row)
    ]
    if len(explicit) != len(identities):
        return Interpretation(
            intent="clarify",
            confidence=0,
            clarification=translate("agent.record_missing", language),
        )
    if explicit:
        context["recent_events"] = explicit
        context["history_truncated"] = False
    selection_invalid = False
    pending = context.get("pending_clarification")
    if not explicit and pending and pending.get("action") in {"update", "close"}:
        identities = pending.get("event_ids", [])
        targets = [row for row in context["recent_events"] if row["id"] in identities]
        if pending.get("explicit_selector") and len(identities) == 1:
            selected = queryable_event(session, UUID(identities[0]))
            if (
                selected is None
                or selected.deleted
                or selected.revision != pending.get("selection_revision")
                or datetime.fromisoformat(pending["selection_expires_at"]) <= now
            ):
                selection_invalid = True
                context["pending_clarification"] = None
                targets = []
            else:
                targets = [serialize(selected)]
        if (
            0 < len(identities) <= 20
            and len(targets) == len(identities)
            and pending.get("targets_complete", len(identities) < 20)
        ):
            context["recent_events"] = targets
            context["history_truncated"] = False
    # Historical source text duplicates payloads and can crowd out the new message.
    for row in context["recent_events"]:
        row.pop("original_text", None)
    payload = {
        "now": now.astimezone(ZoneInfo(settings.timezone)).isoformat(),
        "timezone": settings.timezone,
        "source": source,
        "context": context,
        "text": text,
    }

    def summary(value):
        if isinstance(value, str):
            return value if len(value) <= 300 else value[:300] + " [truncated]"
        if isinstance(value, list):
            return [summary(item) for item in value]
        if isinstance(value, dict):
            return {key: summary(item) for key, item in value.items()}
        return value

    context["recent_events"] = summary(context["recent_events"])
    context["recent_questions"] = summary(context["recent_questions"])
    # All possible targets were loaded above; indicate omissions explicitly.
    context["history_truncated"] = (
        context["history_truncated"] or len(context["recent_events"]) > 20
    )
    context["open_migraine_count"] = sum(
        r["kind"] == "migraine" and (r["end"] is None or datetime.fromisoformat(r["end"]) > now)
        for r in context["recent_events"]
    )
    context["open_episode_counts"] = {
        kind: sum(
            r["kind"] == kind
            and r["status"] == "confirmed"
            and (r["end"] is None or datetime.fromisoformat(r["end"]) > now)
            for r in context["recent_events"]
        )
        for kind in sorted(OPEN_EPISODE_KINDS)
    }
    # Keep the sole closable target of each kind visible even when unrelated
    # legacy open episodes would otherwise consume the bounded prompt.
    context["recent_events"].sort(
        key=lambda row: (
            not (
                row["status"] == "confirmed"
                and context["open_episode_counts"].get(row["kind"]) == 1
                and (row["end"] is None or datetime.fromisoformat(row["end"]) > now)
            )
        )
    )
    context["recent_events"] = context["recent_events"][:20]
    prompt = json.dumps(payload, ensure_ascii=False, default=str)
    if len(prompt) > 24000:
        context["history_truncated"] = True
        context["recent_events"] = []
        # Very long clarification chains remain durable, but cannot crowd out a new message.
        context["pending_clarification"] = None
        prompt = json.dumps(payload, ensure_ascii=False, default=str)
    if len(prompt) > 24000:
        # Retain all potential targets; dropping one could make a close appear unambiguous.
        return Interpretation(
            intent="clarify",
            confidence=0,
            clarification=translate("agent.history_too_large", language),
        )
    if before_model:
        before_model()
    if budget is not None and not budget.consume(EXTRACT_INSTRUCTION, prompt, Interpretation):
        return Interpretation(
            intent="clarify", confidence=0, clarification=budget.exhaustion_notice(language)
        )
    command = provider.structured(EXTRACT_INSTRUCTION, prompt, Interpretation)
    language = command.response_language or language
    if command.intent == "safety":
        return Interpretation(
            intent="safety",
            confidence=command.confidence,
            response_language=command.response_language,
        )
    offered_questions = {UUID(question["id"]) for question in context["recent_questions"]}
    command._question_ids = set(command.question_context_ids)
    if not command._question_ids <= offered_questions:
        raise ValueError("Question dependency outside provided context")
    new_events = command.events if command.intent == "log" else command.events[1:]
    stored = None
    if (
        command.intent in {"update", "close"}
        and command.events
        and command.events[0].payload.type == "medication"
    ):
        from garmin_ai.intake_assertion import unsupported_medication_update

        stored = session.get(Event, command.target_event_id) if command.target_event_id else None
        if stored and unsupported_medication_update(
            command.events[0], command.changed_fields, stored.payload, text
        ):
            return Interpretation(
                intent="clarify",
                confidence=0,
                clarification=translate("agent.medication_update", language),
            )
    from garmin_ai.intake_assertion import invented_unknown_details, resolve_medication_references

    medication_text = resolve_medication_references(
        text, context["recent_events"], now, truncated=context["history_truncated"]
    )
    if medication_text is None:
        if command.intent in {"log", "update", "close", "acknowledge"}:
            return Interpretation(
                intent="clarify",
                confidence=0,
                clarification=translate("agent.medication_repeat", language),
            )
        medication_text = text

    if any(
        event.payload.type == "medication"
        and invented_unknown_details(
            event, medication_text, now, settings.timezone, context.get("pending_clarification")
        )
        for event in new_events
    ):
        return Interpretation(
            intent="clarify",
            confidence=0,
            clarification=translate("agent.medication_details", language),
        )
    medications = [event for event in new_events if event.payload.type == "medication"]
    from garmin_ai.intake_assertion import missing_reported_intakes, without_target_restatement

    coverage_text = medication_text
    if (
        stored
        and command.events
        and (command.events[0].start == stored.start or "start" in command.changed_fields)
    ):
        coverage_text = without_target_restatement(
            medication_text, command.events[0], now, settings.timezone
        )
    if command.intent in {"log", "update", "close", "acknowledge"} and missing_reported_intakes(
        medications,
        coverage_text,
        now,
        settings.timezone,
        context.get("pending_clarification"),
    ):
        return Interpretation(
            intent="clarify",
            confidence=0,
            clarification=translate("agent.medication_intakes", language),
        )
    if command.intent in {"log", "update", "close", "acknowledge"} and medications:
        from garmin_ai.intake_assertion import (
            clarified_intake_times,
            distinct_named_intakes,
            missing_reported_details,
            named_object_order,
        )

        assertion_text = named_object_order(medication_text, medications)
        reported_times = clarified_intake_times(
            assertion_text, now, settings.timezone, context.get("pending_clarification")
        )
        medication_times = [
            event.start for event in new_events if event.payload.type == "medication"
        ]
        if any(
            event.start not in reported_times
            or (
                medication_times.count(event.start) > 1
                and not distinct_named_intakes(
                    [
                        item
                        for item in new_events
                        if item.payload.type == "medication" and item.start == event.start
                    ],
                    assertion_text,
                    now,
                    settings.timezone,
                )
            )
            or missing_reported_details(
                event, medication_text, now, settings.timezone, context.get("pending_clarification")
            )
            for event in medications
        ):
            return Interpretation(
                intent="clarify",
                confidence=0,
                clarification=translate("agent.medication_confirm", language),
            )

    if selection_invalid:
        return Interpretation(
            intent="clarify",
            confidence=0,
            clarification=translate("agent.selection_expired", language),
        )
    pending = context.get("pending_clarification")
    refinement_kinds = set()
    if pending and pending.get("optional_refinement"):
        for identity in pending.get("event_ids", []):
            selected = session.get(Event, UUID(identity), populate_existing=True)
            if selected is not None and not selected.deleted:
                refinement_kinds.add(selected.kind)
    if (
        pending
        and pending.get("optional_refinement")
        and (
            command.intent == "question"
            or (
                command.intent == "log"
                and command.confidence >= 0.85
                and command.events
                and all(
                    bool(refinement_kinds)
                    and event.payload.type not in refinement_kinds
                    and not (
                        event.payload.type == "symptom_observation"
                        and "migraine" in refinement_kinds
                    )
                    and str(getattr(event.payload, "episode_id", None))
                    not in pending.get("event_ids", [])
                    and str(getattr(event.payload, "reason_event_id", None))
                    not in pending.get("event_ids", [])
                    for event in command.events
                )
            )
        )
    ):
        command._dismiss_refinement = True
        context["pending_clarification"] = None
    if command.intent == "acknowledge" and command.target_question_id is None:
        return Interpretation(
            intent="clarify",
            confidence=0,
            clarification=translate("agent.question_episode", language),
        )
    if command.intent == "safety":
        return Interpretation(
            intent="safety",
            confidence=command.confidence,
            response_language=command.response_language,
        )
    if command.confidence < 0.85 and command.intent not in {"question", "clarify", "safety"}:
        command = Interpretation(
            intent="clarify",
            confidence=command.confidence,
            response_language=language,
            clarification=translate("agent.record_details", language),
        )
    single_open_close = command.intent == "close" and any(
        row["id"] == str(command.target_event_id)
        and row["kind"] in OPEN_EPISODE_KINDS
        and row["status"] == "confirmed"
        and context["open_episode_counts"][row["kind"]] == 1
        and (row["end"] is None or datetime.fromisoformat(row["end"]) > now)
        for row in context["recent_events"]
    )
    if (
        command.intent in {"update", "close"}
        and context["history_truncated"]
        and not single_open_close
    ):
        return Interpretation(
            intent="clarify",
            confidence=0,
            clarification=translate("agent.history_target", language),
        )
    # Reject writes referring to a record not actually supplied to the interpreter.
    known = {row["id"]: row for row in context["recent_events"]}
    if command.target_event_id and str(command.target_event_id) not in known:
        if context.get("pending_clarification"):
            return Interpretation(
                intent="clarify",
                confidence=0,
                clarification=translate("agent.selected_record", language),
            )
        raise ValueError("Model selected an event outside the provided context")
    questions = {q["id"]: q for q in context["recent_questions"]}
    if command.target_question_id and str(command.target_question_id) not in questions:
        raise ValueError("Question outside provided context")
    target = str(command.target_event_id) if command.target_event_id else None
    if command.intent == "acknowledge" and command.events and command.target_question_id:
        target = questions[str(command.target_question_id)]["event_id"]
        if target not in known:
            raise ValueError("Acknowledged episode outside provided context")
    if target:
        command._target_revision = known[target]["revision"]
    pending = context.get("pending_clarification")
    if (
        pending
        and pending.get("action") == "update"
        and command.intent in {"log", "update", "close"}
    ):
        linked_symptom_log = (
            command.intent == "log"
            and any(event.payload.type == "symptom_observation" for event in command.events)
            and all(
                (
                    event.payload.type == "symptom_observation"
                    and str(event.payload.episode_id) in pending.get("event_ids", [])
                )
                or (
                    event.payload.type == "medication"
                    and str(event.payload.reason_event_id) in pending.get("event_ids", [])
                )
                for event in command.events
            )
        )
        if not linked_symptom_log and (
            command.intent not in {"update", "close"}
            or str(command.target_event_id) not in pending.get("event_ids", [])
        ):
            return Interpretation(
                intent="clarify",
                confidence=0,
                clarification=translate("agent.update_or_new", language),
            )
    if (
        pending
        and pending.get("action") == "close"
        and command.intent in {"log", "update", "close"}
    ):
        if command.intent != "close" or str(command.target_event_id) not in pending.get(
            "event_ids", []
        ):
            return Interpretation(
                intent="clarify",
                confidence=0,
                clarification=translate("agent.close_episode", language),
            )
    if pending and pending.get("action") == "log" and command.intent in {"log", "update", "close"}:
        expected = {
            "coffee": "caffeine",
            "coffee_preset": "caffeine",
            "migraine": "migraine",
            "alcohol": "alcohol",
            "medication": "medication",
            "note": "note",
        }.get(pending.get("button"))
        if command.intent != "log" or (expected and command.events[0].payload.type != expected):
            return Interpretation(
                intent="clarify",
                confidence=0,
                clarification=translate("agent.button_record", language),
            )
    for index, event in enumerate(command.events):
        correction = (
            index == 0
            and command.intent in {"update", "close", "acknowledge"}
            and not (
                command.intent == "acknowledge" and event.payload.type == "symptom_observation"
            )
        )
        check_start = not correction or "start" in command.changed_fields
        check_end = not correction or "end" in command.changed_fields or command.intent == "close"
        stored_zone = (
            known[target]["timezone"]
            if correction and "timezone" not in command.changed_fields
            else event.timezone
        )
        zone = ZoneInfo(stored_zone)
        for timestamp, checked in ((event.start, check_start), (event.end, check_end)):
            if (
                checked
                and timestamp is not None
                and timestamp.utcoffset() != timestamp.astimezone(zone).utcoffset()
            ):
                return Interpretation(
                    intent="clarify",
                    confidence=0,
                    clarification=translate("agent.timezone_mismatch", language),
                )
            if checked and timestamp is not None:
                wall = timestamp.replace(tzinfo=None)
                candidates = [wall.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
                ambiguous = candidates[0].utcoffset() != candidates[1].utcoffset() and all(
                    c.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall
                    for c in candidates
                )
                offset = timestamp.strftime("%z")
                matches = [
                    match.group("offset").replace(":", "") if match.group("offset") else None
                    for match in re.finditer(
                        r"(?<![\d:+-])(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)(?::[0-5]\d(?:\.\d+)?)?\s*(?:(?:UTC|GMT)\s*)?(?P<offset>[+-]\d{2}:?\d{2})?",
                        text,
                        re.IGNORECASE,
                    )
                    if int(match.group("hour")) == timestamp.hour
                    and int(match.group("minute")) == timestamp.minute
                ]
                explicit_offset = bool(matches) and all(value == offset for value in matches)
                if ambiguous and not explicit_offset:
                    return Interpretation(
                        intent="clarify",
                        confidence=0,
                        clarification=translate("agent.ambiguous_time", language),
                    )
        if (check_start and event.start > now + timedelta(minutes=5)) or (
            check_end and event.end and event.end > now + timedelta(minutes=5)
        ):
            return Interpretation(
                intent="clarify",
                confidence=0,
                clarification=translate("agent.future_time", language),
            )
        if not correction:
            event.status = "confirmed"
        event.source = source
        event.original_text = text
    return command


def apply_command(
    session,
    command: Interpretation,
    *,
    text: str,
    update_id: int | str,
    actor: str,
    now: datetime,
    idempotency_prefix: str | None = None,
    operation_id: UUID | None = None,
):
    idempotency_prefix = idempotency_prefix or f"telegram:{update_id}"
    language = command.response_language or session.info.get("locale", "ru")
    if command._question_ids is not None and command.intent in {
        "log",
        "update",
        "close",
        "acknowledge",
    }:
        question_ids = command._question_ids | (
            {command.target_question_id} if command.target_question_id else set()
        )
        if question_ids:
            from garmin_ai.proactive import reconcile_answers

            lock_writes(session)
            reconcile_answers(session, now)
            session.flush()
            statuses = {"sent", "uncertain", "acknowledged"}
            for identity in question_ids:
                question = session.get(PendingQuestion, identity, populate_existing=True)
                if (
                    question is None
                    or question.status not in statuses
                    or question.expires_at <= now
                    or question.sent_at is None
                    or question.sent_at > now
                ):
                    return translate("agent.diary_question_changed", language)
    if command.target_question_id and command.intent in {"update", "close"}:
        question = session.get(PendingQuestion, command.target_question_id)
        if question is None or (
            question.event_id is not None and question.event_id != command.target_event_id
        ):
            raise ValueError("Follow-up and mutation must identify the same migraine")
    if command.target_question_id and command.intent in {"log", "update", "close", "acknowledge"}:
        question = session.get(PendingQuestion, command.target_question_id, populate_existing=True)
        for event in command.events:
            if event.payload.type == "symptom_observation" and (
                question is None
                or (question.kind == "migraine" and event.payload.episode_id != question.event_id)
            ):
                raise ValueError(
                    "Symptom observation and follow-up must identify the same migraine"
                )
            if event.payload.type == "medication" and (
                question is None
                or (
                    question.kind == "migraine"
                    and event.payload.reason_event_id != question.event_id
                )
            ):
                raise ValueError("Medication and follow-up must identify the same migraine")
    if command.intent == "clarify":
        question = command.clarification or translate("agent.record_fallback", language)
        previous = pending_clarification(session, now)
        selection_prompt = {}
        if previous and previous.value.get("explicit_selector"):
            from garmin_ai.telegram_history import button

            selector = button(
                session,
                session.info.get("conversation_now", now),
                translate("agent.history_button", language),
                "page",
                open_only=previous.value.get("action") == "close",
            )
            session.info["reply_keyboard"] = {"inline_keyboard": [[selector]]}
            selection_prompt = {"selection_prompt": selector["callback_data"]}
        history = list(previous.value.get("messages", [])) if previous else []
        if previous and not history:
            history.append(
                {
                    "text": previous.value.get("text", ""),
                    "question": previous.value.get("question", ""),
                    "at": previous.value.get("created_at"),
                }
            )
        history.append({"text": text, "question": question, "at": now.isoformat()})
        history = history[-8:]
        while (
            len(history) > 1
            and len(json.dumps(history, ensure_ascii=False).encode("utf-8")) > 32000
        ):
            history.pop(0)
        upsert(
            session,
            AppState,
            dict(
                key=pending_key(session),
                value={
                    **(
                        {
                            k: previous.value[k]
                            for k in (
                                "event_ids",
                                "action",
                                "button",
                                "targets_complete",
                                "optional_refinement",
                                "explicit_selector",
                                "selection_revision",
                                "preset_recipe",
                                "preset_selected_at",
                                "selected_at",
                                "selection_expires_at",
                            )
                            if k in previous.value
                        }
                        if previous
                        else {}
                    ),
                    **selection_prompt,
                    "text": text,
                    "question": question,
                    "messages": history,
                    "created_at": session.info.get("conversation_now", now).isoformat(),
                    **(
                        {"channel_instance_id": session.info["channel_destination_instance_id"]}
                        if session.info.get("channel_destination_instance_id")
                        else {}
                    ),
                },
            ),
            ["key"],
        )
        return question
    if command.intent == "acknowledge":
        lock_writes(session)
        question = (
            session.get(PendingQuestion, command.target_question_id, populate_existing=True)
            if command.target_question_id
            else None
        )
        if question is not None and question.kind == "context":
            if command.events:
                raise ValueError("Context facts require log/update, not acknowledgement")
            question.status = "acknowledged"
            question.evidence = {
                **question.evidence,
                "answer_kind": "unknown",
                "answer_text": text,
                "answered_at": now.isoformat(),
            }
            pending = session.get(AppState, pending_key(session))
            if pending:
                session.delete(pending)
            return translate("agent.context_unknown", language)
        if question is None or question.kind != "migraine":
            raise ValueError("Acknowledgement requires a migraine follow-up")
        episode = (
            session.get(Event, question.event_id, populate_existing=True)
            if question.event_id
            else None
        )
        if (
            episode is None
            or episode.deleted
            or episode.kind != "migraine"
            or episode.status != "confirmed"
            or (episode.end is not None and episode.end <= now)
            or episode.start > now
        ):
            question.status = (
                "answered"
                if episode and episode.end and episode.end <= now and not episode.deleted
                else "cancelled"
            )
            return translate("agent.episode_changed", language)
        if command.events:
            if all(event.payload.type == "symptom_observation" for event in command.events):
                combined = command.model_copy(update={"intent": "log", "target_event_id": None})
                return apply_command(
                    session,
                    combined,
                    text=text,
                    update_id=update_id,
                    actor=actor,
                    now=now,
                    idempotency_prefix=idempotency_prefix,
                    operation_id=operation_id,
                )
            if any(event.payload.type == "symptom_observation" for event in command.events):
                raise ValueError("Mixed acknowledgement facts require an explicit log command")
            if not command.changed_fields or command.target_event_id not in {
                None,
                question.event_id,
            }:
                raise ValueError("Acknowledged update must identify the episode and changed fields")
            combined = command.model_copy(
                update={"intent": "update", "target_event_id": question.event_id}
            )
            return apply_command(
                session,
                combined,
                text=text,
                update_id=update_id,
                actor=actor,
                now=now,
                idempotency_prefix=idempotency_prefix,
                operation_id=operation_id,
            )
        question.status = "acknowledged"
        question.evidence = {
            **question.evidence,
            "answer_text": text,
            "answered_at": now.isoformat(),
        }
        pending = session.get(AppState, pending_key(session))
        if pending:
            session.delete(pending)
        return translate("agent.episode_open", language)
    pending = session.get(AppState, pending_key(session))
    if pending:
        session.delete(pending)
    if command.intent == "undo":
        undo_last(session, actor=actor)
        from garmin_ai.proactive import reconcile_answers

        reconcile_answers(session, now)
        return (
            translate("agent.undo_many", language, count=session.info["undo_count"])
            if session.info.get("undo_count", 1) > 1
            else translate("agent.undo_one", language)
        )
    changed = []
    if command.intent == "log":
        changed = create_batch(
            session,
            command.events,
            command.draft_links,
            actor=actor,
            update_id=update_id,
            idempotency_prefix=idempotency_prefix,
            operation_id=operation_id,
        )
    elif command.intent in {"update", "close"}:
        row = session.get(Event, command.target_event_id)
        if not row or row.deleted:
            raise LookupError("Event not found")
        proposed = command.events[0]
        original = {k: v for k, v in serialize(row).items() if k in EventInput.model_fields}
        changes = set(command.changed_fields)
        if command.intent == "close":
            if (
                row.kind not in OPEN_EPISODE_KINDS
                or row.status != "confirmed"
                or proposed.end is None
            ):
                raise ValueError("Close requires an existing episode and end time")
            changes.add("end")
        if not changes:
            raise ValueError("Correction must specify the fields to change")
        values = proposed.model_dump(mode="json")
        for path in changes:
            if path in {"start", "end", "timezone"}:
                original[path] = values[path]
            elif (
                path.startswith("payload.") and path[8:] != "type" and path[8:] in values["payload"]
            ):
                original["payload"][path[8:]] = values["payload"][path[8:]]
            else:
                raise ValueError("Invalid correction field")
        event = EventInput.model_validate(original)
        changed.append(
            update_event(
                session,
                row.id,
                event,
                revision=command._target_revision
                if command._target_revision is not None
                else row.revision,
                actor=actor,
                operation_id=operation_id,
            )
        )
        for index, additional in enumerate(command.events[1:], start=1):
            changed.append(
                create_event(
                    session,
                    additional,
                    actor=actor,
                    idempotency_key=f"{idempotency_prefix}:{index}",
                    operation_id=operation_id,
                )
            )
    else:
        raise ValueError("Not a diary command")
    if command.target_question_id:
        question = session.get(PendingQuestion, command.target_question_id)
        if question is None:
            raise ValueError("Reply must reference an existing follow-up")
        if question.kind == "migraine":
            question.status = "acknowledged"
            question.evidence = {
                **question.evidence,
                "acknowledged_events": {str(row.id): row.revision for row in changed},
                "answer_text": text,
                "answered_at": now.isoformat(),
            }
        elif question.kind == "context":
            question.status = "answered"
            question.evidence = {
                **question.evidence,
                "reply_events": {str(row.id): row.revision for row in changed},
                "answered_at": now.isoformat(),
            }
    from garmin_ai.proactive import reconcile_answers

    reconcile_answers(session, now)
    from garmin_ai.events import headache_observation_label, medication_label

    labels = {
        "wellbeing_observation": translate("agent.saved.wellbeing_observation", language),
        "activity_effort": translate("agent.saved.activity_effort", language),
        "caffeine_log_complete": translate("agent.saved.caffeine_log_complete", language),
        "caffeine": translate("agent.saved.caffeine", language),
        "migraine": translate("agent.saved.migraine", language),
        "symptom_observation": translate("agent.saved.symptom_observation", language),
        "medication": translate("agent.saved.medication", language),
        "context": translate("agent.saved.context", language),
        "note": translate("agent.saved.note", language),
    }
    records = ", ".join(
        f"{headache_observation_label(r.payload, language) if r.kind == 'headache_observation' else medication_label(r.payload, language) if r.kind == 'medication' else labels.get(r.kind, r.kind)} ({r.start.astimezone(ZoneInfo(r.timezone)).strftime('%d.%m %H:%M')})"
        for r in changed
    )
    return translate("agent.saved", language, records=records)


ANSWER_INSTRUCTION = load_prompt("analysis_answer")


ANALYSIS_PROMPT_BYTES = 96000
ANALYSIS_TOTAL_INPUT_BYTES = 384000
ANALYSIS_EVIDENCE_BYTES = 48000
ANALYSIS_TOOL_CALLS = 12
ANALYSIS_SECONDS = 120
ANALYSIS_BUDGET_NOTICE = translate("agent.budget", "ru")
ANALYSIS_TIMEOUT_NOTICE = translate("agent.timeout", "ru")


class AnalysisBudget:
    def __init__(self):
        self.started = monotonic()
        self.model_calls = 0
        self.input_bytes = 0

    def exhaustion_notice(self, language="ru"):
        if monotonic() - self.started >= ANALYSIS_SECONDS:
            return translate("agent.timeout", language)
        return translate("agent.budget", language)

    def consume(self, instruction, prompt, schema):
        size = (
            len(instruction.encode("utf-8"))
            + len(prompt.encode("utf-8"))
            + len(json.dumps(schema.model_json_schema()).encode("utf-8"))
        )
        if (
            self.model_calls >= 6
            or size > ANALYSIS_PROMPT_BYTES
            or self.input_bytes + size > ANALYSIS_TOTAL_INPUT_BYTES
            or monotonic() - self.started >= ANALYSIS_SECONDS
        ):
            return False
        self.model_calls += 1
        self.input_bytes += size
        return True


def selected_timing_read(step):
    """Optimize a typed point read selected by the model, independent of wording."""
    if step.urgent_safety or len(step.calls) != 1:
        return None
    call = step.calls[0]
    if call.name != "analysis_sleep":
        return None
    try:
        arguments = TOOLS[call.name].arguments.model_validate_json(call.arguments_json)
    except ValueError:
        return None
    if (
        not arguments.timing_only
        or arguments.start != arguments.end
        or arguments.nap_policy != "separate"
    ):
        return None
    return arguments.start


def answer_question(
    session,
    provider: Provider,
    text: str,
    settings: Settings,
    now: datetime,
    before_model=None,
    *,
    update_id=None,
    reply_to_message_id=None,
    budget=None,
):
    from garmin_ai.accounts import effective_owner_settings
    from garmin_ai.conversation import (
        conversation_context,
        epoch_matches,
        remember_answer,
    )

    settings = effective_owner_settings(session, settings)
    language = settings.locale
    session.info["model_provider_instance_id"] = getattr(
        provider,
        "instance_id",
        session.info.get("model_provider_instance_id", "model:gemini:primary"),
    )
    session.info["analysis_reply"] = True
    budget = budget if budget is not None else AnalysisBudget()
    if provider is None:
        raise ProviderUnavailable("Natural-language questions require a model provider")
    # Plan from bounded conversational context and tool contracts before touching
    # global freshness or replay-dependent health projections.
    conversation = conversation_context(session, now, reply_to_message_id, maintain=False)
    session.info["analysis_epoch"] = conversation["epoch"]
    if conversation["selection_missing"]:
        screened = screen_reply_safety(provider, text, before_model, locale=settings.locale)
        language = screened.response_language or settings.locale
        if screened.intent == "safety":
            from garmin_ai.diary_forms import urgent_notice

            return screened.clarification or urgent_notice(language)
        if screened.clarification:
            return screened.clarification
        return translate("agent.reply_context_missing", language)
    session.info["timezone"] = settings.timezone
    evidence = []
    tool_calls = 0
    from garmin_ai.access import TOOL_SCOPES, required_tool_scopes
    from garmin_ai.personal_goals import preferences, revision_matches
    from garmin_ai.replay import replay_generation, replay_pending_condition

    goal_selection = preferences(session, bind_owner=False)
    session.info["goals_revision"] = None
    # Re-query source evidence; prior answers are neither fresh facts nor a
    # reason to resend recorded health values before access is revalidated.
    model_conversation = {
        key: value for key, value in conversation.items() if key not in {"epoch", "turns"}
    }
    model_conversation["turns"] = [
        {key: turn[key] for key in ("question", "asked_at", "specs") if key in turn}
        for turn in conversation["turns"]
        if "question" in turn
    ]
    descriptions = [
        {"name": t.name, "description": t.description, "schema": t.arguments.model_json_schema()}
        for t in TOOLS.values()
    ]
    prompt = json.dumps(
        {
            "now": now.astimezone(ZoneInfo(settings.timezone)).isoformat(),
            "timezone": settings.timezone,
            "question": text,
            "conversation": model_conversation,
            "quality_context": {},
            "tools": descriptions,
            "personal_goals": goal_selection,
            "remaining_tool_rounds": max(0, 5 - budget.model_calls),
            "remaining_tool_calls": ANALYSIS_TOOL_CALLS,
            "answer_only": False,
            "planning_only": True,
            "evidence": [],
        },
        ensure_ascii=False,
    )
    if before_model:
        before_model()
    if preferences(session, bind_owner=False)["revision"] != goal_selection["revision"]:
        return translate("agent.goals_changed", language)
    if not epoch_matches(session, conversation["epoch"]):
        return translate("agent.conversation_deleted", language)
    if before_model:
        session.commit()
    if not budget.consume(ANSWER_INSTRUCTION, prompt, AgentStep):
        return budget.exhaustion_notice(language)
    first_step = provider.structured(ANSWER_INSTRUCTION, prompt, AgentStep)
    language = first_step.response_language or language
    if first_step.urgent_safety:
        from garmin_ai.diary_forms import urgent_notice

        session.info["analysis_projection"] = None
        return urgent_notice(first_step.response_language or settings.locale)
    if preferences(session, bind_owner=False)["revision"] != goal_selection["revision"]:
        return translate("agent.goals_changed", language)
    if not epoch_matches(session, conversation["epoch"]):
        return translate("agent.conversation_deleted", language)
    timing_read = selected_timing_read(first_step)
    if timing_read is not None:
        from garmin_ai.db import writer_guard
        from garmin_ai.sleep_timing import timing_projection_current, timing_result_hash

        session.info["sleep_timing_fact"] = True
        if monotonic() - budget.started >= ANALYSIS_SECONDS:
            return translate("agent.timeout", language)
        day = timing_read
        arguments = {"start": str(day), "end": str(day), "timing_only": True}
        writer_guard(session)
        try:
            result = call_tool(session, "analysis_sleep", arguments, for_model=True)
        except ReplayUnavailable:
            return translate("agent.replay", language)
        except (ValueError, PermissionError):
            return translate("agent.sleep_unavailable", language)
        evidence = [{"id": 1, "tool": "analysis_sleep", "arguments": arguments, "result": result}]
        tool_calls = 1
        snapshot = {
            "day": str(day),
            "timezone": settings.timezone,
            "hash": timing_result_hash(result),
        }

        def timing_current():
            try:
                return timing_projection_current(session, snapshot, for_model=True)
            except PermissionError:
                return False

        followup = json.loads(prompt)
        followup.update(
            planning_only=False,
            evidence=evidence,
            remaining_tool_rounds=max(0, 5 - budget.model_calls),
            remaining_tool_calls=ANALYSIS_TOOL_CALLS - tool_calls,
        )
        prompt = json.dumps(followup, ensure_ascii=False)
        if before_model:
            before_model()
        if preferences(session, bind_owner=False)["revision"] != goal_selection["revision"]:
            return translate("agent.goals_changed", language)
        if not epoch_matches(session, conversation["epoch"]):
            return translate("agent.conversation_deleted", language)
        if not timing_current():
            return translate("agent.data_changed", language)
        if before_model:
            session.commit()
        if not budget.consume(ANSWER_INSTRUCTION, prompt, AgentStep):
            return budget.exhaustion_notice(language)
        first_step = provider.structured(ANSWER_INSTRUCTION, prompt, AgentStep)
        language = first_step.response_language or language
        if first_step.urgent_safety:
            from garmin_ai.diary_forms import urgent_notice

            session.info["analysis_projection"] = None
            return urgent_notice(language)
        point_answer = (
            not first_step.calls
            and (
                bool(first_step.timestamp_claims)
                if result["status"] == "available"
                else bool(first_step.answer) and not first_step.timestamp_claims
            )
            and not first_step.numeric_claims
            and not first_step.include_current_state_freshness
            and set(first_step.evidence_ids) == {1}
            and all(
                claim.path
                in (["rows", 0, "main_interval", "start"], ["rows", 0, "main_interval", "end"])
                for claim in first_step.timestamp_claims
            )
        )
        if point_answer and result["status"] != "available":
            response = translate(
                "agent.sleep_unconfirmed",
                language,
                day=f"{day:%d.%m.%Y}" if language.split("-")[0] == "ru" else day.isoformat(),
            )
        elif point_answer:
            try:
                timestamps = verified_timestamps(
                    first_step.timestamp_claims,
                    evidence,
                    {1},
                    settings.timezone,
                    locale=language,
                )
            except ValueError:
                return translate("agent.numbers_unverified", language)
            # A typed point answer needs only the verified fields. Do not copy
            # model prose, which may repeat timestamps or add unsupported facts.
            response = "\n".join(timestamps)

        if preferences(session, bind_owner=False)["revision"] != goal_selection["revision"]:
            return translate("agent.goals_changed", language)
        if not epoch_matches(session, conversation["epoch"]):
            return translate("agent.conversation_deleted", language)
        if not timing_current():
            return translate("agent.data_changed", language)
        if point_answer:
            response += translate("agent.saved_garmin", language)
            # Never retain the global replay fence across a model request.
            retain_answer = update_id is not None and session.scalar(
                select(func.pg_try_advisory_xact_lock(72104619))
            )
            session.info["analysis_projection"] = {
                "generation": replay_generation(session) if retain_answer else None,
                "model_provider_instance_id": session.info["model_provider_instance_id"],
                "sleep_timing": snapshot,
            }
            session.info["goals_revision"] = goal_selection["revision"]
            if retain_answer:
                remember_answer(
                    session, now, update_id, text, response, evidence, epoch=conversation["epoch"]
                )
            return response
        # A broader follow-up plan returns to the ordinary loop with its existing
        # evidence and model step, without repeating either request.
        session.info.pop("sleep_timing_fact", None)

    from garmin_ai.queries import data_freshness

    preferences(session)  # Bind ownership without replacing the planned goal revision.
    initial_replay_generation = replay_generation(session)
    # Capture the generation before revalidating the independent point result.
    # Later generation checks close the handoff without holding a shared writer
    # fence that would need upgrading when the answer is retained.
    if timing_read is not None and not timing_current():
        return translate("agent.data_changed", language)
    from garmin_ai.analysis_dependencies import metric_dependency
    from garmin_ai.sleep_timing import timing_dependencies_current, timing_result_hash

    def dependency_projection(selected_ids=None):
        dependencies = [
            {
                "day": item["arguments"]["start"],
                "timezone": settings.timezone,
                "hash": timing_result_hash(item["result"]),
            }
            for item in evidence
            if (selected_ids is None or item["id"] in selected_ids)
            and item["tool"] == "analysis_sleep"
            and item["result"].get("algorithm_version") == "main-sleep-timing-v1"
            and item["arguments"].get("timing_only") is True
            and item["arguments"].get("start") == item["arguments"].get("end")
        ]
        metrics = [
            dependency
            for item in evidence
            if (selected_ids is None or item["id"] in selected_ids)
            and (dependency := metric_dependency(item, now)) is not None
        ]
        return {
            "generation": initial_replay_generation,
            **({"generic_metric_dependencies": metrics} if metrics else {}),
            **(
                {
                    "model_provider_instance_id": session.info["model_provider_instance_id"],
                    **({"sleep_timing_dependencies": dependencies} if dependencies else {}),
                }
                if dependencies or metrics
                else {}
            ),
        }

    def dependencies_current(selected_ids=None):
        try:
            return timing_dependencies_current(
                session, dependency_projection(selected_ids), for_model=True
            )
        except (PermissionError, KeyError, TypeError, ValueError):
            return False

    was_replaying = False

    def replay_safe(item):
        name = item.get("tool", item.get("name"))
        return name == "data_freshness" or "read:health" not in TOOL_SCOPES.get(name, set())

    def replay_evidence(items):
        from garmin_ai.tools import model_freshness

        return [
            {**item, "result": model_freshness(session, data_freshness(session, now=now))}
            if item.get("tool", item.get("name")) == "data_freshness"
            else item
            for item in items
            if replay_safe(item)
        ]

    for turn in range(6):
        answer_only = turn == 5 or budget.model_calls >= 5 or tool_calls >= ANALYSIS_TOOL_CALLS
        replaying = bool(session.scalar(select(replay_pending_condition())))
        if was_replaying and not replaying:
            initial_replay_generation = replay_generation(session)
            evidence = replay_evidence(evidence)
        was_replaying = replaying
        if replaying:
            quality_context = {}
        else:
            from garmin_ai.scenario_packs import pack_enabled
            from garmin_ai.tools import model_metric_packs

            quality_context = {
                metric: channel
                for metric, channel in data_freshness(session, now=now)["channels"].items()
                if all(pack_enabled(session, pack, "llm") for pack in model_metric_packs(metric))
            }
        available_tools = [item for item in descriptions if not replaying or replay_safe(item)]
        if replaying:
            evidence = replay_evidence(evidence)
            conversation = {**conversation, "turns": []}
        session.info["analysis_projection"] = None if replaying else dependency_projection()
        prompt = json.dumps(
            {
                "now": now.astimezone(ZoneInfo(settings.timezone)).isoformat(),
                "timezone": settings.timezone,
                "question": text,
                "conversation": {**model_conversation, "turns": []}
                if replaying
                else model_conversation,
                "quality_context": quality_context,
                "tools": [] if answer_only else available_tools,
                "personal_goals": goal_selection,
                "remaining_tool_rounds": 0 if answer_only else max(0, 5 - budget.model_calls),
                "remaining_tool_calls": max(0, ANALYSIS_TOOL_CALLS - tool_calls),
                "answer_only": answer_only,
                "garmin_replay_notice": translate("agent.replay", language) if replaying else None,
                "evidence": evidence,
            },
            ensure_ascii=False,
        )
        if before_model:
            before_model()
        if not revision_matches(session, goal_selection["revision"]):
            return translate("agent.goals_changed", language)
        if not epoch_matches(session, conversation["epoch"]):
            return translate("agent.conversation_deleted", language)
        if not dependencies_current():
            session.info["analysis_projection"] = None
            return translate("agent.data_changed", language)
        if before_model:
            session.commit()  # Release the epoch read transaction before network I/O.
        if turn == 0:
            step = first_step
        else:
            if not budget.consume(ANSWER_INSTRUCTION, prompt, AgentStep):
                return budget.exhaustion_notice(language)
            step = provider.structured(ANSWER_INSTRUCTION, prompt, AgentStep)
        language = step.response_language or language
        if replaying and any(not replay_safe({"name": call.name}) for call in step.calls):
            return translate("agent.replay", language)
        if step.urgent_safety:
            session.info["analysis_projection"] = None
            from garmin_ai.diary_forms import urgent_notice

            return urgent_notice(language)
        if not replaying and replay_generation(session) != initial_replay_generation:
            return translate("agent.recalculated", language)
        if not revision_matches(
            session,
            goal_selection["revision"],
            lock=bool(
                (step.answer or step.numeric_claims or step.timestamp_claims) and not step.calls
            ),
        ):
            return translate("agent.goals_changed", language)
        if not epoch_matches(
            session,
            conversation["epoch"],
            lock=bool(
                (step.answer or step.numeric_claims or step.timestamp_claims) and not step.calls
            ),
        ):
            return translate("agent.conversation_deleted", language)
        selected_ids = (
            set(step.evidence_ids)
            if (step.answer or step.numeric_claims or step.timestamp_claims) and not step.calls
            else None
        )
        if not dependencies_current(selected_ids):
            session.info["analysis_projection"] = None
            return translate("agent.data_changed", language)
        if (step.answer or step.numeric_claims or step.timestamp_claims) and not step.calls:
            if replaying and step.include_current_state_freshness:
                return translate("agent.replay", language)
            if session.scalar(select(replay_pending_condition())):
                if not replaying:
                    return translate("agent.replay", language)
                evidence = replay_evidence(evidence)
            valid = {e["id"] for e in evidence if "error" not in e["result"]}
            if not evidence or not step.evidence_ids or not set(step.evidence_ids) <= valid:
                return translate("agent.answer_unverified", language)
            try:
                if any(character.isnumeric() for character in (step.answer or "")):
                    raise ValueError("Unverified numeric narrative")
                numbers = verified_numbers(step.numeric_claims, evidence, set(step.evidence_ids))
                timestamps = verified_timestamps(
                    step.timestamp_claims,
                    evidence,
                    set(step.evidence_ids),
                    settings.timezone,
                    locale=language,
                )
            except ValueError:
                return translate("agent.numbers_unverified", language)
            from garmin_ai.freshness import render_current_state_freshness

            freshness = (
                render_current_state_freshness(quality_context, now, settings.timezone, language)
                if step.include_current_state_freshness
                else ""
            )
            response = "\n\n".join(
                part
                for part in [freshness, step.answer, "\n".join(numbers), "\n".join(timestamps)]
                if part
            )
            used_scopes = set()
            for item in evidence:
                if item["id"] not in step.evidence_ids:
                    continue
                if item["tool"] != "resource_catalog":
                    used_scopes.update(TOOL_SCOPES.get(item["tool"], set()))
                else:
                    try:
                        arguments = TOOLS["resource_catalog"].arguments.model_validate(
                            item.get("arguments", {})
                        )
                    except ValueError:
                        # Older saved evidence can lack arguments; keep its conservative
                        # source attribution instead of failing the completed answer.
                        used_scopes.update(TOOL_SCOPES["resource_catalog"])
                    else:
                        used_scopes.update(required_tool_scopes("resource_catalog", arguments))
            sources = [
                label
                for scope, label in (
                    ("read:health", "Garmin"),
                    ("read:diary", translate("agent.source.diary", language)),
                )
                if scope in used_scopes
            ]
            response += (
                translate("agent.saved_sources", language, sources=" and ".join(sources))
                if sources and language.split("-")[0] != "ru"
                else translate("agent.saved_sources", language, sources=" и ".join(sources))
                if sources
                else translate("agent.saved_data", language)
            )
            session.info["analysis_projection"] = (
                None if replaying else dependency_projection(set(step.evidence_ids))
            )
            session.info["goals_revision"] = goal_selection["revision"]
            remember_answer(
                session, now, update_id, text, response, evidence, epoch=conversation["epoch"]
            )
            return response
        if not step.calls or answer_only:
            break
        for call in step.calls:
            if tool_calls >= ANALYSIS_TOOL_CALLS:
                break
            if monotonic() - budget.started >= ANALYSIS_SECONDS:
                return translate("agent.timeout", language)
            tool_calls += 1
            arguments = {}
            try:
                arguments = json.loads(call.arguments_json)
                previous_llm_access = session.info.get("llm_access")
                session.info["llm_access"] = True
                try:
                    result = call_tool(session, call.name, arguments)
                finally:
                    if previous_llm_access is None:
                        session.info.pop("llm_access", None)
                    else:
                        session.info["llm_access"] = previous_llm_access
                value = json.loads(compact(result))
            except (ValueError, LookupError, PermissionError, TypeError):
                value = {"error": "Invalid tool arguments; inspect schema and retry"}
            item = {
                "id": max((item["id"] for item in evidence), default=0) + 1,
                "tool": call.name,
                "result": value,
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
            if (
                len(json.dumps([*evidence, item], ensure_ascii=False).encode("utf-8"))
                > ANALYSIS_EVIDENCE_BYTES
            ):
                return translate("agent.budget", language)
            evidence.append(item)
    if tool_calls >= ANALYSIS_TOOL_CALLS:
        return translate("agent.budget", language)
    return translate("agent.steps_exhausted", language)
