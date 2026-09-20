"""Trusted first-party scenario packs and independent owner preferences."""

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import Field
from sqlalchemy import func, select, update

from garmin_ai.accounts import owner
from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.models import (
    Activity,
    AppState,
    ChannelBinding,
    Event,
    HealthDay,
    Measurement,
    MetricObservation,
    ModuleConfig,
    PendingQuestion,
    SourceConnection,
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
        frozenset({"wellbeing_check_in"}),
        frozenset({"wellbeing_summary", "stress_trends"}),
    ),
    "sleep": ScenarioPack(
        "sleep",
        {"en": "Sleep", "ru": "Сон"},
        frozenset({"nap"}),
        frozenset(),
        frozenset({"sleep_check_in"}),
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
        frozenset({"effort_check_in"}),
        frozenset({"training_trends"}),
    ),
}

QUESTION_PACK = {"caffeine": "caffeine", "migraine": "migraine", "context": "wellbeing"}


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
            SourceConnection,
            ChannelBinding,
        )
    )
    return populated or session.get(AppState, "preferences:personal-goals") is not None


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
    if not row.reminders_enabled:
        kinds = [kind for kind, pack in QUESTION_PACK.items() if pack == key]
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


def llm_allows_question(session, kind: str) -> bool:
    pack = QUESTION_PACK.get(kind)
    return pack is None or pack_enabled(session, pack, "llm")
