"""Trusted first-party scenario packs and independent owner preferences."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import Field
from sqlalchemy import and_, func, select, update

from garmin_ai.accounts import owner
from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.models import (
    Activity,
    AppState,
    Conversation,
    Event,
    HealthDay,
    Insight,
    Measurement,
    MetricObservation,
    ModuleConfig,
    PendingQuestion,
    SourcePayload,
    TimelineInterval,
)


@dataclass(frozen=True)
class ScenarioPack:
    key: str
    labels: dict[str, str]
    definitions: frozenset[str]
    forms: frozenset[str]
    rules: frozenset[str]
    analysis: frozenset[str]


PACKS = {
    "general_diary": ScenarioPack(
        "general_diary",
        {"en": "General diary", "ru": "Общий дневник"},
        frozenset(
            {
                "alcohol",
                "context",
                "hydration",
                "illness",
                "meal",
                "mood",
                "note",
                "stressor",
                "travel",
            }
        ),
        frozenset({"note"}),
        frozenset({"context_follow_up"}),
        frozenset({"history"}),
    ),
    "wellbeing": ScenarioPack(
        "wellbeing",
        {"en": "Wellbeing", "ru": "Самочувствие"},
        frozenset({"wellbeing_observation"}),
        frozenset({"wellbeing"}),
        frozenset({"context_follow_up"}),
        frozenset({"wellbeing_summary", "stress_trends"}),
    ),
    "sleep": ScenarioPack(
        "sleep",
        {"en": "Sleep", "ru": "Сон"},
        frozenset({"nap"}),
        frozenset(),
        frozenset(),
        frozenset({"sleep_trends"}),
    ),
    "caffeine": ScenarioPack(
        "caffeine",
        {"en": "Caffeine", "ru": "Кофеин"},
        frozenset({"caffeine", "caffeine_absence", "caffeine_log_complete"}),
        frozenset({"coffee", "coffee_preset"}),
        frozenset({"caffeine_follow_up"}),
        frozenset({"caffeine_timing"}),
    ),
    "migraine": ScenarioPack(
        "migraine",
        {"en": "Migraine", "ru": "Мигрень"},
        frozenset({"headache_observation", "medication", "migraine", "symptom_observation"}),
        frozenset({"medication", "migraine", "migraine_end"}),
        frozenset({"migraine_follow_up"}),
        frozenset({"migraine_windows", "migraine_comparison"}),
    ),
    "training": ScenarioPack(
        "training",
        {"en": "Training", "ru": "Тренировки"},
        frozenset({"activity_effort"}),
        frozenset({"activity_effort"}),
        frozenset(),
        frozenset({"training_trends"}),
    ),
}

QUESTION_PACK = {"caffeine": "caffeine", "migraine": "migraine", "context": "wellbeing"}

# A source response is archived in full. Mixed or unclassified responses require
# every pack whose facts they may contain to permit collection.
GARMIN_ENDPOINT_PACKS = {
    "daily": ("training", "wellbeing"),
    "steps": ("training",),
    "heart_rate": ("wellbeing",),
    "sleep": ("sleep",),
    "hrv": ("wellbeing",),
    "stress": ("wellbeing",),
    "body_battery": ("wellbeing",),
    "body_battery_events": ("wellbeing",),
    "respiration": ("wellbeing",),
    "spo2": ("wellbeing",),
    "readiness": ("training",),
    "training_status": ("training",),
    "max_metrics": ("training",),
    "endurance": ("training",),
    "hill": ("training",),
    "hydration": ("general_diary",),
    "body_composition": ("wellbeing",),
    "intensity": ("training",),
    "resting_hr": ("wellbeing",),
    "all_day_events": ("general_diary", "wellbeing", "sleep", "training"),
    "devices": ("training",),
    "activities": ("training",),
    "activity_fit": ("training",),
}


def garmin_collection_enabled(session, endpoint: str) -> bool:
    packs = GARMIN_ENDPOINT_PACKS.get(endpoint)
    if packs is None and (endpoint == "activity" or endpoint.startswith("activity_")):
        packs = ("training",)
    if packs is None:
        raise ValueError(f"Unknown Garmin endpoint: {endpoint}")
    return all(pack_enabled(session, pack, "collection") for pack in packs)


class PackSelection(StrictModel):
    revision: int = Field(ge=1, strict=True)
    tracking_enabled: bool
    collection_enabled: bool
    reminders_enabled: bool
    visible: bool
    llm_enabled: bool
    outcome_goal: str | None = Field(default=None, max_length=500)


def _has_legacy_footprint(session) -> bool:
    populated = any(
        session.scalar(select(1).select_from(table).limit(1)) is not None
        for table in (
            Event,
            Activity,
            HealthDay,
            Measurement,
            MetricObservation,
            SourcePayload,
            TimelineInterval,
        )
    )
    return (
        populated
        or session.get(AppState, "preferences:personal-goals") is not None
        or session.get(AppState, "migration:legacy-scenario-packs") is not None
    )


def ensure_scenario_packs(session, *, legacy_install: bool | None = None):
    """Create explicit defaults once; absent rows retain pre-migration legacy behavior."""
    person = owner(session)
    existing = {
        row.pack_key: row
        for row in session.scalars(select(ModuleConfig).where(ModuleConfig.owner_id == person.id))
    }
    if existing:
        return existing
    legacy_install = _has_legacy_footprint(session) if legacy_install is None else legacy_install
    for key in PACKS:
        enabled = legacy_install or key == "general_diary"
        session.add(
            ModuleConfig(
                owner_id=person.id,
                pack_key=key,
                tracking_enabled=enabled,
                collection_enabled=enabled,
                reminders_enabled=enabled,
                visible=enabled,
                llm_enabled=legacy_install,
            )
        )
    session.flush()
    return {
        row.pack_key: row
        for row in session.scalars(select(ModuleConfig).where(ModuleConfig.owner_id == person.id))
    }


def pack_enabled(session, key: str, capability: str = "tracking") -> bool:
    if key not in PACKS:
        raise ValueError("Unknown scenario pack")
    column = {
        "tracking": ModuleConfig.tracking_enabled,
        "collection": ModuleConfig.collection_enabled,
        "reminders": ModuleConfig.reminders_enabled,
        "visibility": ModuleConfig.visible,
        "llm": ModuleConfig.llm_enabled,
    }.get(capability)
    if column is None:
        raise ValueError("Unknown pack capability")
    person = owner(session)
    count = session.scalar(
        select(func.count()).select_from(ModuleConfig).where(ModuleConfig.owner_id == person.id)
    )
    if not count:
        return True
    value = session.scalar(
        select(column).where(ModuleConfig.owner_id == person.id, ModuleConfig.pack_key == key)
    )
    return bool(value)


def list_scenario_packs(session):
    rows = ensure_scenario_packs(session)
    return [pack_state(rows[key]) for key in PACKS]


def pack_state(row):
    pack = PACKS[row.pack_key]
    return {
        "key": row.pack_key,
        "labels": pack.labels,
        "definitions": sorted(pack.definitions),
        "forms": sorted(pack.forms),
        "rules": sorted(pack.rules),
        "analysis": sorted(pack.analysis),
        "tracking_enabled": row.tracking_enabled,
        "collection_enabled": row.collection_enabled,
        "reminders_enabled": row.reminders_enabled,
        "visible": row.visible,
        "llm_enabled": row.llm_enabled,
        "outcome_goal": row.outcome_goal,
        "revision": row.revision,
    }


def configure_scenario_pack(session, key: str, selection: PackSelection):
    if key not in PACKS:
        raise LookupError("Scenario pack not found")
    selection = PackSelection.model_validate(selection)
    lock_writes(session)
    rows = ensure_scenario_packs(session)
    row = session.scalar(
        select(ModuleConfig).where(ModuleConfig.id == rows[key].id).with_for_update()
    )
    if row.revision != selection.revision:
        raise Conflict("Scenario pack changed; reload before editing")
    if row.llm_enabled and not selection.llm_enabled:
        # Stored answers can contain this pack's facts even after future tool access
        # is denied. The conversation has no per-turn pack provenance.
        from garmin_ai.conversation import forget_conversation

        forget_conversation(session)
        for conversation in session.scalars(select(Conversation).with_for_update()):
            conversation.memory_epoch = uuid4()
            conversation.state = {}
        pending = session.get(AppState, "conversation:pending", populate_existing=True)
        if pending and pending.value.get("pack") in {None, key}:
            session.delete(pending)
    for field in (
        "tracking_enabled",
        "collection_enabled",
        "reminders_enabled",
        "visible",
        "llm_enabled",
        "outcome_goal",
    ):
        setattr(row, field, getattr(selection, field))
    row.revision += 1
    row.updated_at = datetime.now(UTC)
    if not row.reminders_enabled or (key in {"caffeine", "migraine"} and not row.tracking_enabled):
        kinds = [kind for kind, pack in QUESTION_PACK.items() if pack == key]
        if key == "general_diary":
            kinds.append("context")
        if kinds:
            session.execute(
                update(PendingQuestion)
                .where(
                    PendingQuestion.kind.in_(kinds),
                    PendingQuestion.status.in_(["pending", "sending", "sent", "uncertain"]),
                )
                .values(status="cancelled")
            )
    session.flush()
    return pack_state(row)


def event_pack(kind: str) -> str | None:
    return next((key for key, pack in PACKS.items() if kind in pack.definitions), None)


def llm_allows_event(session, kind: str) -> bool:
    pack = event_pack(kind.removeprefix("system."))
    return pack is None or pack_enabled(session, pack, "llm")


def llm_event_filter(session):
    """SQL predicate for events permitted in model-visible result sets."""
    disallowed = {
        kind
        for key, pack in PACKS.items()
        if not pack_enabled(session, key, "llm")
        for kind in pack.definitions
    }
    return Event.kind.not_in(disallowed) if disallowed else Event.kind.is_not(None)


def llm_allows_question(session, kind: str) -> bool:
    return question_enabled(session, kind, "llm")


def question_enabled(session, kind: str, capability: str) -> bool:
    pack = QUESTION_PACK.get(kind)
    if pack is not None and not pack_enabled(session, pack, capability):
        return False
    if kind in {"caffeine", "migraine"} and capability == "reminders":
        return pack_enabled(session, kind, "tracking")
    if kind == "context":
        return pack_enabled(session, "general_diary", capability) and (
            capability != "reminders" or pack_enabled(session, "general_diary", "tracking")
        )
    return True


def insight_pack(insight) -> str | None:
    try:
        metric = insight.dedup_key.split(":", 2)[1]
    except (AttributeError, IndexError):
        return None
    return "sleep" if metric in {"sleep_score", "sleep_seconds"} else "wellbeing"


def insight_enabled(session, insight) -> bool:
    pack = insight_pack(insight)
    return pack is None or (
        pack_enabled(session, pack) and pack_enabled(session, pack, "reminders")
    )


def insight_filter(session):
    predicates = []
    metric_packs = {
        "sleep": ("sleep_score", "sleep_seconds"),
        "wellbeing": ("hrv_nightly_avg", "resting_hr", "stress_avg"),
    }
    for pack, metrics in metric_packs.items():
        if not (pack_enabled(session, pack) and pack_enabled(session, pack, "reminders")):
            predicates.extend(Insight.dedup_key.not_like(f"trend:{metric}:%") for metric in metrics)
    return and_(*predicates) if predicates else Insight.id.is_not(None)
