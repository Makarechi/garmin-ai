import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationInfo, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.models import Audit, Event, Insight, PendingQuestion


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
    name: str = Field(min_length=1, max_length=200)
    dose: float = Field(gt=0, le=100000)
    unit: Literal["mg", "mcg", "g", "ml", "tablet", "drop", "IU"]
    reason_event_id: UUID | None = None


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
    | SymptomObservation
    | WellbeingObservation,
    Field(discriminator="type"),
]


class EventInput(StrictModel):
    start: AwareDatetime
    end: AwareDatetime | None = None
    timezone: str = "Europe/Bratislava"
    source: Literal[
        "manual", "telegram_text", "telegram_button", "telegram_voice", "mcp", "inferred"
    ] = "manual"
    confidence: float = Field(default=1, ge=0, le=1)
    status: Literal["confirmed", "inferred", "needs_confirmation"] = "confirmed"
    original_text: str | None = Field(default=None, max_length=16000)
    payload: Payload

    @model_validator(mode="after")
    def valid_interval(self, info: ValidationInfo):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown timezone") from None
        if self.payload.type == "symptom_observation" and self.end not in {None, self.start}:
            raise ValueError("Symptom observation describes one recorded instant")
        if self.payload.type == "wellbeing_observation":
            if self.end not in {None, self.start}:
                raise ValueError("Wellbeing observations are point-in-time reports")
            self.end = None
        if self.payload.type == "caffeine_absence" and self.end is None:
            raise ValueError("Caffeine absence requires an end")
        if self.payload.type in {"headache_observation", "caffeine_log_complete"} and (
            self.end is None or self.end <= self.start
        ):
            raise ValueError("Coverage observation requires a nonempty covered interval")
        if self.end and self.end < self.start:
            raise ValueError("End must not precede start")
        if (
            self.payload.type == "wellbeing_observation"
            and (self.source == "inferred" or self.status == "inferred")
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
    if row.end is None:
        return "open_interval" if row.kind in OPEN_EPISODE_KINDS else "point"
    return "point" if row.end == row.start else "bounded_interval"


def event_overlap(start: datetime, end: datetime):
    """SQL predicate for overlap with [start, end); callers choose status/deletion policy."""
    return and_(
        Event.start < end,
        or_(
            Event.end > start,
            and_(Event.end.is_(None), Event.kind.in_(OPEN_EPISODE_KINDS)),
            and_(or_(Event.end.is_(None), Event.end == Event.start), Event.start >= start),
        ),
    )


def serialize_event(row) -> dict:
    topology = event_topology(row)
    return {
        **serialize(row),
        "topology": topology,
        # Ongoing means no recorded end, not proof of symptoms at the current instant.
        "ongoing": topology == "open_interval",
        "missing_end": topology == "open_interval",
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
    validate_relation(session, event)
    stmt = insert(Event).values(**values, idempotency_key=idempotency_key)
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
    row.revision += 1
    session.flush()
    invalidate_migraine_insights(session, before["kind"], row.kind)
    sync_migraine_questions(session, row, before)
    session.add(
        Audit(event_id=row.id, action="update", before=before, after=serialize(row), actor=actor)
    )
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
    before = serialize(row)
    ensure_unreferenced(session, row.id)
    row.deleted = True
    row.revision += 1
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
        ):
            setattr(row, key, audit.before[key])
        row.start = datetime.fromisoformat(audit.before["start"])
        row.end = datetime.fromisoformat(audit.before["end"]) if audit.before["end"] else None
    row.revision += 1
    session.flush()
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
