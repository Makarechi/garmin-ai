import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.models import Audit, Event, PendingQuestion


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Caffeine(StrictModel):
    type: Literal["caffeine"] = "caffeine"
    beverage: str = Field(min_length=1, max_length=200)
    servings: float = Field(default=1, gt=0, le=30)
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


class Migraine(StrictModel):
    type: Literal["migraine"] = "migraine"
    severity: int | None = Field(default=None, ge=0, le=10)
    aura: bool | None = None
    symptoms: list[str] = Field(default_factory=list, max_length=30)
    notes: str | None = Field(default=None, max_length=4000)


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
    ]
    description: str = Field(min_length=1, max_length=4000)
    amount: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=30)


Payload = Annotated[Caffeine | Migraine | Medication | ContextEvent, Field(discriminator="type")]


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
    def valid_interval(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown timezone") from None
        if self.end and self.end < self.start:
            raise ValueError("End must not precede start")
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


def event_values(event: EventInput) -> dict:
    event = EventInput.model_validate(event.model_dump())
    values = event.model_dump(exclude={"payload"})
    return {**values, "kind": event.payload.type, "payload": event.payload.model_dump(mode="json")}


def validate_relation(session, event: EventInput):
    if isinstance(event.payload, Medication) and event.payload.reason_event_id:
        related = session.get(Event, event.payload.reason_event_id, populate_existing=True)
        if not related or related.deleted or related.kind != "migraine":
            raise ValueError("Medication relation must reference an existing migraine")


def lock_writes(session):
    # Single-owner database: serialize mutations and undo across all actors.
    session.execute(select(func.pg_advisory_xact_lock(72104619)))


def ensure_unreferenced(session, event_id):
    linked = session.scalar(
        select(Event.id)
        .where(
            Event.kind == "medication",
            Event.deleted.is_(False),
            Event.payload["reason_event_id"].astext == str(event_id),
        )
        .limit(1)
    )
    if linked:
        raise Conflict("Detach linked medication before removing or changing this migraine")


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
        if key in {"start", "end"}:
            recorded = datetime.fromisoformat(recorded).astimezone(UTC) if recorded else None
            value = value.astimezone(UTC) if value else None
        if recorded != value:
            raise Conflict("Idempotency key already used for different data")
    return existing


def create_event(session, event: EventInput, *, actor: str, idempotency_key: str | None = None):
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
    session.add(
        Audit(event_id=row.id, action="create", before=None, after=serialize(row), actor=actor)
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
    if isinstance(event.payload, Medication) and event.payload.reason_event_id == row.id:
        raise Conflict("A medication cannot reference itself")
    validate_relation(session, event)
    if row.kind == "migraine" and event.payload.type != "migraine":
        ensure_unreferenced(session, row.id)
    before = serialize(row)
    for key, value in event_values(event).items():
        setattr(row, key, value)
    row.revision += 1
    session.flush()
    reopen_questions(session, row, before)
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
        if not audit.before["deleted"]:
            restored = EventInput.model_validate(
                {key: audit.before[key] for key in EventInput.model_fields}
            )
            validate_relation(session, restored)
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
    reopen_questions(session, row, before)
    session.add(
        Audit(event_id=row.id, action="undo", before=before, after=serialize(row), actor=actor)
    )
    return row


def reopen_questions(session, row, before):
    if (
        before["end"]
        and row.kind == "migraine"
        and row.end is None
        and not row.deleted
        and row.status == "confirmed"
    ):
        for question in session.scalars(
            select(PendingQuestion).where(
                PendingQuestion.kind == "migraine",
                PendingQuestion.event_id == row.id,
                PendingQuestion.status == "answered",
            )
        ):
            question.status = "sent" if question.sent_at else "pending"
            question.evidence = {
                key: value for key, value in question.evidence.items() if key != "answer_event_id"
            }
