"""Canonical single-owner store; health data is never stored in the LLM context."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SourcePayload(Base):
    __tablename__ = "source_payloads"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(default="garmin_connect")
    endpoint: Mapped[str]
    source_key: Mapped[str]
    payload_hash: Mapped[str]
    payload: Mapped[dict | list | None] = mapped_column(JSONB)
    archive_key: Mapped[str]
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parser_version: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="pending")
    __table_args__ = (UniqueConstraint("source", "endpoint", "source_key", "payload_hash"),)


class Measurement(Base):
    __tablename__ = "measurements"
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    metric: Mapped[str] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(primary_key=True, default="garmin_connect")
    local_date: Mapped[date] = mapped_column(index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str]
    source_ref: Mapped[uuid.UUID | None] = mapped_column(UUID, index=True)
    quality: Mapped[str] = mapped_column(default="observed")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class HealthDay(Base):
    __tablename__ = "health_days"
    day: Mapped[date] = mapped_column(primary_key=True)
    sleep_score: Mapped[float | None]
    sleep_seconds: Mapped[float | None]
    deep_seconds: Mapped[float | None]
    rem_seconds: Mapped[float | None]
    light_seconds: Mapped[float | None]
    awake_seconds: Mapped[float | None]
    hrv_nightly_avg: Mapped[float | None]
    hrv_weekly_avg: Mapped[float | None]
    hrv_baseline_low: Mapped[float | None]
    hrv_baseline_high: Mapped[float | None]
    hrv_status: Mapped[str | None]
    resting_hr: Mapped[float | None]
    body_battery_high: Mapped[float | None]
    body_battery_low: Mapped[float | None]
    body_battery_charged: Mapped[float | None]
    body_battery_drained: Mapped[float | None]
    stress_avg: Mapped[float | None]
    stress_max: Mapped[float | None]
    steps: Mapped[int | None]
    active_calories: Mapped[float | None]
    intensity_minutes: Mapped[float | None]
    training_readiness_score: Mapped[float | None]
    recovery_time_minutes: Mapped[float | None]
    training_status: Mapped[str | None]
    vo2max: Mapped[float | None]
    sources: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str]
    name: Mapped[str | None]
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str]
    duration_seconds: Mapped[float | None]
    moving_seconds: Mapped[float | None]
    distance_m: Mapped[float | None]
    avg_hr: Mapped[float | None]
    max_hr: Mapped[float | None]
    avg_speed_mps: Mapped[float | None]
    calories: Mapped[float | None]
    cadence: Mapped[float | None]
    ascent_m: Mapped[float | None]
    descent_m: Mapped[float | None]
    aerobic_effect: Mapped[float | None]
    anaerobic_effect: Mapped[float | None]
    training_load: Mapped[float | None]
    fit_key: Mapped[str | None]
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (CheckConstraint('"end" >= start'),)


class ActivityPart(Base):
    __tablename__ = "activity_parts"
    activity_id: Mapped[str] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(index=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str]
    source: Mapped[str]
    confidence: Mapped[float] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="confirmed")
    original_text: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint('"end" IS NULL OR "end" >= start'),
        CheckConstraint("confidence >= 0 AND confidence <= 1"),
    )


class Audit(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID, index=True)
    action: Mapped[str]
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    actor: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TimelineInterval(Base):
    __tablename__ = "timeline_intervals"
    id: Mapped[str] = mapped_column(primary_key=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    label: Mapped[str]
    source: Mapped[str]
    confidence: Mapped[float]
    confirmed: Mapped[bool] = mapped_column(default=False)
    evidence: Mapped[dict] = mapped_column(JSONB)


class PendingQuestion(Base):
    __tablename__ = "pending_questions"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str]
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    text: Mapped[str]
    evidence: Mapped[dict] = mapped_column(JSONB)
    priority: Mapped[float]
    earliest_send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(default="pending")
    dedup_key: Mapped[str] = mapped_column(unique=True)
    attempts: Mapped[int] = mapped_column(default=0)


class Insight(Base):
    __tablename__ = "insights"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    category: Mapped[str]
    statement: Mapped[str]
    evidence: Mapped[dict] = mapped_column(JSONB)
    sample_size: Mapped[int]
    effect_size: Mapped[float | None]
    status: Mapped[str] = mapped_column(default="candidate")
    dedup_key: Mapped[str] = mapped_column(unique=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSONB)
    dedup_key: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="pending", index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppState(Base):
    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(default="pending")


Index("ix_job_due", Job.status, Job.run_at)
