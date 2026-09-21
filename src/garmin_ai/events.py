import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationInfo, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.models import (
    Activity,
    Audit,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    Insight,
    MetricObservation,
    PendingQuestion,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Caffeine(StrictModel):
    type: Literal["caffeine"] = "caffeine"
    beverage: str = Field(min_length=1, max_length=200)
    servings: float = Field(default=1, gt=0, le=30)
    dose_basis: Literal["total", "per_serving", "unknown"] = "unknown"
    dose_provenance: Literal["estimated", "reported_label", "unknown"] = "unknown"
    dose_notes: str | None = Field(default=None, max_length=500)
    caffeine_mg_estimate: float | None = Field(default=None, ge=0, le=5000)
    caffeine_mg_min: float | None = Field(default=None, ge=0, le=5000)
    caffeine_mg_max: float | None = Field(default=None, ge=0, le=5000)

    @model_validator(mode="after")
    def valid_range(self):
        low, mid, high = self.caffeine_mg_min, self.caffeine_mg_estimate, self.caffeine_mg_max
        if low is not None and high is not None and low > high:
            raise ValueError("Invalid caffeine range")
        if mid is not None and (
            (low is not None and mid < low) or (high is not None and mid > high)
        ):
            raise ValueError("Estimate outside range")
        return self


def caffeine_total(payload):
    """Resolve total milligrams only when the stored dose basis is explicit."""
    basis = payload.get("dose_basis", "unknown")
    factor = payload.get("servings", 1) if basis == "per_serving" else 1
    known = basis in {"total", "per_serving"}
    values = {
        field: payload.get("caffeine_mg_" + field) * factor
        if known and payload.get("caffeine_mg_" + field) is not None
        else None
        for field in ("min", "estimate", "max")
    }
    return {
        **values,
        "unit": "mg",
        "basis": "total",
        "source_basis": basis,
        "provenance": payload.get("dose_provenance", "unknown"),
        "status": "available" if any(value is not None for value in values.values()) else "unknown",
    }


class Migraine(StrictModel):
    type: Literal["migraine"] = "migraine"
    severity: int | None = Field(default=None, ge=0, le=10)
    aura: bool | None = None
    symptoms: list[str] = Field(default_factory=list, max_length=30)
    notes: str | None = Field(default=None, max_length=4000)


class SymptomObservation(StrictModel):
    type: Literal["symptom_observation"] = "symptom_observation"
    episode_id: UUID
    severity: int | None = Field(default=None, ge=0, le=10)
    aura: bool | None = None
    symptoms: list[str] = Field(default_factory=list, max_length=30)
    impact: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def has_observation(self):
        if self.severity is None and self.aura is None and not self.symptoms and not self.impact:
            raise ValueError("At least one reported symptom observation is required")
        return self


class Medication(StrictModel):
    type: Literal["medication"] = "medication"
    name: str | None = Field(default=None, min_length=1, max_length=200)
    dose: float | None = Field(default=None, gt=0, le=100000)
    unit: Literal["mg", "mcg", "g", "ml", "tablet", "drop", "IU"] | None = None
    reason_event_id: UUID | None = None


def medication_label(payload):
    name = payload.get("name") or "название неизвестно"
    dose = str(payload["dose"]) if payload.get("dose") is not None else "доза неизвестна"
    unit = payload.get("unit") or "единица неизвестна"
    return f"Лекарство: {name}, {dose} {unit}"


class ActivityEffort(StrictModel):
    type: Literal["activity_effort"] = "activity_effort"
    activity_id: str = Field(min_length=1, max_length=100)
    perceived_exertion: int = Field(ge=0, le=10, strict=True)
    notes: str | None = Field(default=None, max_length=2000)


class ContextEvent(StrictModel):
    type: Literal[
        "alcohol",
        "meal",
        "hydration",
        "illness",
        "nap",
        "stressor",
        "travel",
        "mood",
        "note",
        "context",
        "caffeine_absence",
        "caffeine_log_complete",
    ]
    description: str = Field(min_length=1, max_length=4000)
    amount: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=30)


def headache_observation_label(payload):
    labels = {"yes": "да", "no": "нет", "unknown": "неизвестно"}
    return f"Головная боль: {labels[payload['headache']]}; мигрень: {labels[payload['migraine']]}"


class HeadacheObservation(StrictModel):
    type: Literal["headache_observation"] = "headache_observation"
    headache: Literal["yes", "no", "unknown"]
    migraine: Literal["yes", "no", "unknown"]


class WellbeingObservation(StrictModel):
    type: Literal["wellbeing_observation"] = "wellbeing_observation"
    energy: int | None = Field(default=None, ge=0, le=10, strict=True)
    restedness: int | None = Field(default=None, ge=0, le=10, strict=True)
    pain: int | None = Field(default=None, ge=0, le=10, strict=True)
    functional_impact: int | None = Field(default=None, ge=0, le=10, strict=True)
    notes: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def has_observation(self):
        if all(
            getattr(self, field) is None
            for field in ("energy", "restedness", "pain", "functional_impact", "notes")
        ):
            raise ValueError("Provide at least one reported wellbeing observation")
        if self.notes is not None and not self.notes.strip():
            raise ValueError("Wellbeing notes cannot be blank")
        return self


Payload = Annotated[
    Caffeine
    | Migraine
    | Medication
    | ContextEvent
    | HeadacheObservation
    | WellbeingObservation
    | ActivityEffort
    | SymptomObservation,
    Field(discriminator="type"),
]


class EventInput(StrictModel):
    start: AwareDatetime
    end: AwareDatetime | None = None
    timezone: str = "Europe/Bratislava"
    source: Literal[
        "manual",
        "telegram_text",
        "telegram_button",
        "telegram_voice",
        "mcp",
        "inferred",
        "wearable",
    ] = "manual"
    confidence: float = Field(default=1, ge=0, le=1)
    status: Literal["confirmed", "inferred", "needs_confirmation"] = "confirmed"
    original_text: str | None = Field(default=None, max_length=16000)
    payload: Payload

    @model_validator(mode="after")
    def valid_interval(self, info: ValidationInfo):
        if (
            self.payload.type == "medication"
            and any(getattr(self.payload, field) is None for field in ("name", "dose", "unit"))
            and (self.source in {"inferred", "wearable"} or self.status != "confirmed")
        ):
            raise ValueError("Incomplete medication requires a confirmed reported intake")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown timezone") from None
        if self.payload.type in {"wellbeing_observation", "activity_effort"}:
            if self.end not in {None, self.start}:
                raise ValueError("Wellbeing observations are point-in-time reports")
            self.end = None
        if self.payload.type == "symptom_observation" and self.end not in {None, self.start}:
            raise ValueError("Symptom observation describes one recorded instant")
        if self.payload.type == "caffeine_absence" and (self.end is None or self.end <= self.start):
            raise ValueError("Caffeine absence requires an end after its start")
        if self.payload.type in {"headache_observation", "caffeine_log_complete"} and (
            self.end is None or self.end <= self.start
        ):
            raise ValueError("Coverage observation requires a nonempty covered interval")
        if self.end and self.end < self.start:
            raise ValueError("End must not precede start")
        if (
            self.payload.type in {"wellbeing_observation", "activity_effort"}
            and (self.source in {"inferred", "wearable"} or self.status == "inferred")
            and not (info.context or {}).get("restore_audited_snapshot")
        ):
            raise ValueError("Wellbeing observations require explicit user reports")
        if self.source == "inferred" and self.status == "confirmed":
            raise ValueError("Inferred data cannot be marked confirmed without user action")
        return self


def serialize(row) -> dict:
    result = {col.name: getattr(row, col.name) for col in row.__table__.columns}
    return json.loads(
        json.dumps(
            result,
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        )
    )


class Conflict(ValueError):
    pass


OPEN_EPISODE_KINDS = frozenset({"migraine", "illness"})


def event_topology(row) -> str:
    """Interpret legacy rows without inventing a recorded end or changing stored facts."""
    if getattr(row, "topology", None):
        return row.topology
    if row.end is None:
        return "open_interval" if row.kind in OPEN_EPISODE_KINDS else "point"
    return "point" if row.end == row.start else "bounded_interval"


def event_overlap(start: datetime, end: datetime):
    """SQL predicate for overlap with [start, end); callers choose status/deletion policy."""
    return and_(
        Event.start < end,
        or_(
            Event.end > start,
            and_(Event.end.is_(None), Event.topology == "open_interval"),
            and_(
                or_(Event.end.is_(None), Event.end == Event.start),
                Event.topology.in_(["point", "flexible"]),
                Event.start >= start,
            ),
        ),
    )


def event_query_allowed():
    queryable = select(EventDefinitionVersion.id).where(
        EventDefinitionVersion.allowed_operations.contains(["query"])
    )
    return or_(
        Event.definition_version_id.is_(None),
        Event.definition_version_id.in_(queryable),
    )


def serialize_event(row) -> dict:
    from garmin_ai.canonical_events import canonical_envelope

    topology = event_topology(row)
    return {
        **serialize(row),
        "topology": topology,
        "canonical": canonical_envelope(row),
        # Ongoing means no recorded end, not proof of symptoms at the current instant.
        "ongoing": topology == "open_interval" and row.end is None,
        "missing_end": topology == "open_interval" and row.end is None,
        **({"caffeine_total": caffeine_total(row.payload)} if row.kind == "caffeine" else {}),
    }


def invalidate_migraine_insights(session, *kinds):
    if not {"migraine", "headache_observation", "symptom_observation"}.intersection(kinds):
        return
    session.execute(
        update(Insight)
        .where(
            or_(
                Insight.category.in_(["migraine", "migraine_comparison"]),
                Insight.evidence["tool"].astext == "analysis_migraine_windows",
            ),
            Insight.status != "superseded",
        )
        .values(status="superseded")
    )


def event_values(event: EventInput) -> dict:
    event = EventInput.model_validate(event.model_dump())
    values = event.model_dump(exclude={"payload"})
    return {**values, "kind": event.payload.type, "payload": event.payload.model_dump(mode="json")}


def validate_relation(session, event: EventInput):
    if isinstance(event.payload, ActivityEffort):
        activity = session.get(Activity, event.payload.activity_id)
        if activity is None:
            raise ValueError("Effort report must identify an existing activity")
        if event.start.astimezone(UTC) < activity.start:
            raise ValueError("Effort report cannot precede its activity")
    if isinstance(event.payload, SymptomObservation):
        related = session.get(Event, event.payload.episode_id, populate_existing=True)
        if (
            not related
            or related.deleted
            or related.kind != "migraine"
            or related.status != "confirmed"
        ):
            raise ValueError("Symptom observation must reference an existing confirmed migraine")
        instant = event.start.astimezone(UTC)
        if instant < related.start or (related.end is not None and instant > related.end):
            raise ValueError("Symptom timestamp must fall within its migraine episode")
    if isinstance(event.payload, Medication) and event.payload.reason_event_id:
        related = session.get(Event, event.payload.reason_event_id, populate_existing=True)
        if not related or related.deleted or related.kind != "migraine":
            raise ValueError("Medication relation must reference an existing migraine")


def lock_writes(session):
    from garmin_ai.db import writer_guard

    writer_guard(session)
    # Single-owner database: serialize mutations and undo across all actors.
    session.execute(select(func.pg_advisory_xact_lock(72104619)))


def ensure_unreferenced(session, event_id, *, symptoms_only=False):
    linked = session.scalar(
        select(Event.id)
        .where(
            Event.deleted.is_(False),
            Event.kind == "symptom_observation" if symptoms_only else True,
            or_(
                and_(
                    Event.kind == "medication",
                    Event.payload["reason_event_id"].astext == str(event_id),
                ),
                and_(
                    Event.kind == "symptom_observation",
                    Event.payload["episode_id"].astext == str(event_id),
                ),
            ),
        )
        .limit(1)
    )
    if linked:
        raise Conflict(
            "Detach linked medication or symptom observations before removing or changing this migraine"
        )


def validate_symptom_bounds(session, event_id, event):
    if event.payload.type != "migraine":
        return
    outside = session.scalar(
        select(Event.id)
        .where(
            Event.deleted.is_(False),
            Event.kind == "symptom_observation",
            Event.payload["episode_id"].astext == str(event_id),
            or_(
                Event.start < event.start,
                Event.start > event.end if event.end is not None else False,
            ),
        )
        .limit(1)
    )
    if outside:
        raise Conflict("Episode bounds would strand linked symptom observations")


def replay_matches(session, existing, values):
    original = session.scalar(
        select(Audit)
        .where(Audit.event_id == existing.id, Audit.action == "create")
        .order_by(Audit.id)
        .limit(1)
    )
    if original is None:
        raise Conflict("Creation audit unavailable for idempotent replay")
    for key, value in values.items():
        recorded = original.after[key]
        if (
            key == "payload"
            and value.get("type") == "caffeine"
            and recorded.get("type") == "caffeine"
        ):
            recorded = Caffeine.model_validate(recorded).model_dump(mode="json")
        if key in {"start", "end"}:
            recorded = datetime.fromisoformat(recorded).astimezone(UTC) if recorded else None
            value = value.astimezone(UTC) if value else None
        if isinstance(value, UUID) and isinstance(recorded, str):
            recorded = UUID(recorded)
        if recorded != value:
            raise Conflict("Idempotency key already used for different data")
    return existing


def create_event(
    session,
    event: EventInput,
    *,
    actor: str,
    idempotency_key: str | None = None,
    operation_id: UUID | None = None,
):
    event = EventInput.model_validate(event.model_dump())
    lock_writes(session)
    values = event_values(event)
    if idempotency_key is not None:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("Invalid idempotency key")
        existing = session.scalar(
            select(Event)
            .where(Event.idempotency_key == idempotency_key)
            .execution_options(populate_existing=True)
        )
        if existing:
            return replay_matches(session, existing, values)
    from garmin_ai.scenario_packs import event_pack, pack_enabled

    pack = event_pack(event.payload.type)
    capability = "collection" if event.source == "wearable" else "tracking"
    if pack is not None and not pack_enabled(session, pack, capability):
        raise PermissionError(f"The {pack} scenario pack is disabled")
    from garmin_ai.definitions import ensure_system_definition

    definition_version = ensure_system_definition(session, event.payload.type)
    topology = definition_version.topology
    if topology in {"flexible", "open_interval"}:
        topology = (
            "open_interval"
            if definition_version.topology == "open_interval" and event.end is None
            else "bounded_interval"
            if event.end is not None and event.end > event.start
            else "point"
        )
    from garmin_ai.canonical_events import provenance_values

    canonical = provenance_values(event.source, event.status, topology=topology, actor=actor)
    validate_relation(session, event)
    stmt = insert(Event).values(
        **values,
        definition_version_id=definition_version.id,
        topology=topology,
        **canonical,
        idempotency_key=idempotency_key,
    )
    if idempotency_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Event.idempotency_key])
    event_id = session.scalar(stmt.returning(Event.id))
    if event_id is None:
        existing = session.scalar(
            select(Event)
            .where(Event.idempotency_key == idempotency_key)
            .execution_options(populate_existing=True)
        )
        return replay_matches(session, existing, values)
    row = session.get(Event, event_id)
    invalidate_migraine_insights(session, row.kind)
    session.add(
        Audit(
            event_id=row.id,
            action="create",
            before=None,
            after=serialize(row),
            actor=actor,
            operation_id=operation_id,
        )
    )
    from garmin_ai.metric_definitions import project_event_metrics

    project_event_metrics(session, row)
    return row


def update_event(session, event_id: UUID, event: EventInput, *, revision: int, actor: str):
    event = EventInput.model_validate(event.model_dump())
    lock_writes(session)
    row = session.scalar(
        select(Event)
        .where(Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.deleted:
        raise LookupError("Event not found")
    if row.revision != revision:
        raise Conflict("Event changed; reload before editing")
    from garmin_ai.scenario_packs import event_pack, pack_enabled

    destination_pack = event_pack(event.payload.type)
    if (
        event.payload.type != row.kind
        and destination_pack is not None
        and not pack_enabled(session, destination_pack, "tracking")
    ):
        raise PermissionError(f"The {destination_pack} scenario pack is disabled")
    if row.definition_version_id is not None:
        bound_version = session.get(EventDefinitionVersion, row.definition_version_id)
        bound_definition = (
            session.get(EventDefinition, bound_version.definition_id) if bound_version else None
        )
        if bound_definition is not None and bound_definition.namespace == "user":
            raise ValueError("Custom entries must use the custom correction endpoint")
    if isinstance(event.payload, SymptomObservation) and event.payload.episode_id == row.id:
        raise Conflict("A symptom observation cannot reference itself")
    if isinstance(event.payload, Medication) and event.payload.reason_event_id == row.id:
        raise Conflict("A medication cannot reference itself")
    validate_relation(session, event)
    if row.kind == "migraine" and event.payload.type != "migraine":
        ensure_unreferenced(session, row.id)
    elif row.kind == "migraine" and event.status != "confirmed":
        ensure_unreferenced(session, row.id, symptoms_only=True)
    validate_symptom_bounds(session, row.id, event)
    before = serialize(row)
    for key, value in event_values(event).items():
        setattr(row, key, value)
    from garmin_ai.definitions import ensure_system_definition

    row.definition_version_id = ensure_system_definition(session, event.payload.type).id
    version = session.get(EventDefinitionVersion, row.definition_version_id)
    row.topology = version.topology
    if row.topology in {"flexible", "open_interval"}:
        row.topology = (
            "open_interval"
            if version.topology == "open_interval" and event.end is None
            else "bounded_interval"
            if event.end is not None and event.end > event.start
            else "point"
        )
    from garmin_ai.canonical_events import provenance_values

    for key, value in provenance_values(
        event.source, event.status, topology=row.topology, actor=actor
    ).items():
        setattr(row, key, value)
    row.revision += 1
    session.flush()
    invalidate_migraine_insights(session, before["kind"], row.kind)
    sync_migraine_questions(session, row, before)
    session.add(
        Audit(event_id=row.id, action="update", before=before, after=serialize(row), actor=actor)
    )
    from garmin_ai.metric_definitions import project_event_metrics

    project_event_metrics(session, row, rebuild=True)
    return row


def delete_event(session, event_id: UUID, *, revision: int, actor: str):
    lock_writes(session)
    row = session.scalar(
        select(Event)
        .where(Event.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.deleted:
        raise LookupError("Event not found")
    if row.revision != revision:
        raise Conflict("Event changed; reload before deleting")
    if row.definition_version_id is not None:
        version = session.get(EventDefinitionVersion, row.definition_version_id)
        if version is not None and "delete" not in version.allowed_operations:
            raise PermissionError("Definition does not allow deletion")
    before = serialize(row)
    ensure_unreferenced(session, row.id)
    row.deleted = True
    row.revision += 1
    session.execute(
        update(MetricObservation)
        .where(
            MetricObservation.source_entry_id == row.id,
            MetricObservation.valid.is_(True),
        )
        .values(valid=False, invalidated_at=datetime.now(UTC))
    )
    session.flush()
    invalidate_migraine_insights(session, row.kind)
    sync_migraine_questions(session, row, before)
    session.add(
        Audit(event_id=row.id, action="delete", before=before, after=serialize(row), actor=actor)
    )
    return row


def undo_last(session, *, actor: str):
    lock_writes(session)
    # Lock serializes undo with other changes for this owner.
    audit = session.scalar(
        select(Audit)
        .where(Audit.actor == actor, Audit.action != "undo")
        .order_by(Audit.id.desc())
        .limit(1)
    )
    if audit is None:
        raise LookupError("Nothing to undo")
    audits = [audit]
    if audit.operation_id is not None:
        audits = session.scalars(
            select(Audit)
            .where(
                Audit.operation_id == audit.operation_id,
                Audit.actor == actor,
                Audit.action != "undo",
            )
            .order_by(Audit.id.desc())
        ).all()
    with session.begin_nested():
        changed = [_undo_audit(session, item, actor) for item in audits]
    session.info["undo_count"] = len(changed)
    return changed[0]


def _undo_audit(session, audit, actor):
    row = session.scalar(
        select(Event)
        .where(Event.id == audit.event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or audit.after["revision"] != row.revision:
        raise Conflict("The last operation has already been changed or undone")
    before = serialize(row)
    if row.kind == "migraine" and (
        audit.before is None or audit.before["kind"] != "migraine" or audit.before["deleted"]
    ):
        ensure_unreferenced(session, row.id)
    if audit.before is None:
        ensure_unreferenced(session, row.id)
        row.deleted = True
    else:
        if row.kind == "migraine" and audit.before["status"] != "confirmed":
            ensure_unreferenced(session, row.id, symptoms_only=True)
        if not audit.before["deleted"]:
            before_version_id = audit.before.get("definition_version_id")
            before_version = (
                session.get(EventDefinitionVersion, UUID(before_version_id))
                if before_version_id
                else None
            )
            before_definition = (
                session.get(EventDefinition, before_version.definition_id)
                if before_version
                else None
            )
            if before_definition is not None and before_definition.namespace == "user":
                from garmin_ai.definitions import validate_values

                validate_values(
                    before_version,
                    {key: value for key, value in audit.before["payload"].items() if key != "type"},
                )
            else:
                restored = EventInput.model_validate(
                    {key: audit.before[key] for key in EventInput.model_fields},
                    context={"restore_audited_snapshot": True},
                )
                validate_relation(session, restored)
                validate_symptom_bounds(session, row.id, restored)
        for key in (
            "kind",
            "timezone",
            "source",
            "confidence",
            "status",
            "original_text",
            "payload",
            "deleted",
            "envelope_version",
            "time_precision",
            "assertion_kind",
            "producer",
            "transport",
            "author",
            "evidence_refs",
            "validation_status",
        ):
            if key in audit.before:
                setattr(row, key, audit.before[key])
        for key in ("recorded_at", "ingested_at"):
            if audit.before.get(key):
                setattr(row, key, datetime.fromisoformat(audit.before[key]))
        row.start = datetime.fromisoformat(audit.before["start"])
        row.end = datetime.fromisoformat(audit.before["end"]) if audit.before["end"] else None
        if audit.before.get("definition_version_id"):
            row.definition_version_id = UUID(audit.before["definition_version_id"])
        else:
            from garmin_ai.definitions import ensure_system_definition

            row.definition_version_id = ensure_system_definition(session, row.kind).id
        if audit.before.get("topology"):
            row.topology = audit.before["topology"]
        elif row.end is None and row.kind in OPEN_EPISODE_KINDS:
            row.topology = "open_interval"
        elif row.end is None or row.end == row.start:
            row.topology = "point"
        else:
            row.topology = "bounded_interval"
    row.revision += 1
    session.flush()
    if row.definition_version_id is not None:
        from garmin_ai.metric_definitions import project_event_metrics

        if row.deleted:
            session.execute(
                update(MetricObservation)
                .where(
                    MetricObservation.source_entry_id == row.id,
                    MetricObservation.valid.is_(True),
                )
                .values(valid=False, invalidated_at=datetime.now(UTC))
            )
        else:
            project_event_metrics(session, row, rebuild=True)
    invalidate_migraine_insights(session, before["kind"], row.kind)
    sync_migraine_questions(session, row, before)
    session.add(
        Audit(
            event_id=row.id,
            action="undo",
            before=before,
            after=serialize(row),
            actor=actor,
            operation_id=audit.operation_id,
        )
    )
    return row


def sync_migraine_questions(session, row, before):
    now = datetime.now(UTC)
    from garmin_ai.scenario_packs import pack_enabled

    if not pack_enabled(session, "migraine", "reminders"):
        for question in session.scalars(
            select(PendingQuestion).where(
                PendingQuestion.kind == "migraine",
                PendingQuestion.event_id == row.id,
                PendingQuestion.status.in_(
                    ["pending", "sending", "sent", "uncertain", "acknowledged"]
                ),
            )
        ):
            question.status = "cancelled"
        return
    if row.kind != "migraine" or row.deleted or row.status != "confirmed":
        for question in session.scalars(
            select(PendingQuestion).where(
                PendingQuestion.kind == "migraine",
                PendingQuestion.event_id == row.id,
                PendingQuestion.status.in_(
                    ["pending", "sending", "sent", "uncertain", "acknowledged"]
                ),
            )
        ):
            question.status = "cancelled"
        return
    if row.kind == "migraine" and row.end is not None and row.end <= now and not row.deleted:
        for question in session.scalars(
            select(PendingQuestion).where(
                PendingQuestion.kind == "migraine",
                PendingQuestion.event_id == row.id,
                PendingQuestion.status.in_(
                    ["pending", "sending", "sent", "uncertain", "acknowledged"]
                ),
            )
        ):
            question.status = "answered"
            question.evidence = {**question.evidence, "answer_event_id": str(row.id)}
    if (
        (
            before["end"]
            or before["deleted"]
            or before["kind"] != "migraine"
            or before["status"] != "confirmed"
        )
        and row.kind == "migraine"
        and (row.end is None or row.end > now)
        and not row.deleted
        and row.status == "confirmed"
    ):
        for question in session.scalars(
            select(PendingQuestion).where(
                PendingQuestion.kind == "migraine",
                PendingQuestion.event_id == row.id,
                PendingQuestion.status.in_(["answered", "cancelled"]),
            )
        ):
            reactivate_question(question, datetime.now(UTC))


def reactivate_question(question, now):
    question.status = "sent" if question.sent_at else "pending"
    question.evidence = {
        key: value
        for key, value in question.evidence.items()
        if key not in {"answer_event_id", "answer_text", "answered_at", "acknowledged_events"}
    }
    if question.expires_at <= now:
        question.expires_at = now + timedelta(days=2)
        question.earliest_send_at = now
