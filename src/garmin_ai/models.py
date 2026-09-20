"""Canonical single-owner store; model prompts receive bounded query results."""

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


class Person(Base):
    __tablename__ = "people"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    singleton: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    locale: Mapped[str] = mapped_column(default="ru")
    timezone: Mapped[str] = mapped_column(default="Europe/Bratislava")
    units: Mapped[str] = mapped_column(default="metric")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint("singleton", name="ck_people_single_owner"),
        CheckConstraint("units IN ('metric', 'imperial')", name="ck_people_units"),
        UniqueConstraint("singleton", name="uq_people_singleton"),
    )


class SourceConnection(Base):
    __tablename__ = "source_connections"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str]
    namespace: Mapped[str]
    external_id: Mapped[str]
    confirmation_method: Mapped[str]
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint("owner_id", "provider", "namespace", name="uq_owner_source_namespace"),
        UniqueConstraint(
            "provider", "namespace", "external_id", name="uq_source_external_identity"
        ),
    )


class ChannelBinding(Base):
    __tablename__ = "channel_bindings"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str]
    channel_instance_id: Mapped[str]
    external_id: Mapped[str]
    confirmation_method: Mapped[str]
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "channel", "channel_instance_id", name="uq_owner_channel_instance"
        ),
        UniqueConstraint(
            "channel",
            "channel_instance_id",
            "external_id",
            name="uq_channel_external_identity",
        ),
    )


class EventDefinition(Base):
    __tablename__ = "event_definitions"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    namespace: Mapped[str]
    key: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="draft")
    revision: Mapped[int] = mapped_column(default=1)
    current_version: Mapped[int | None]
    draft: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'proposed', 'active', 'retired')",
            name="ck_event_definitions_status",
        ),
        CheckConstraint("revision >= 1", name="ck_event_definitions_revision"),
        CheckConstraint(
            "(namespace = 'system' AND owner_id IS NULL) OR "
            "(namespace = 'user' AND owner_id IS NOT NULL)",
            name="ck_event_definitions_namespace_owner",
        ),
    )


class EventDefinitionVersion(Base):
    __tablename__ = "event_definition_versions"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("event_definitions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int]
    schema: Mapped[dict] = mapped_column(JSONB)
    schema_hash: Mapped[str]
    topology: Mapped[str]
    field_metadata: Mapped[dict] = mapped_column(JSONB)
    labels: Mapped[dict] = mapped_column(JSONB)
    privacy: Mapped[str]
    allowed_operations: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("definition_id", "version", name="uq_event_definition_version"),
        CheckConstraint("version >= 1", name="ck_event_definition_versions_version"),
        CheckConstraint(
            "topology IN ('point', 'open_interval', 'bounded_interval', 'flexible')",
            name="ck_event_definition_versions_topology",
        ),
        CheckConstraint(
            "privacy IN ('private', 'sensitive')",
            name="ck_event_definition_versions_privacy",
        ),
    )


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    namespace: Mapped[str]
    key: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="active")
    current_version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint(
            "(namespace = 'system' AND owner_id IS NULL) OR "
            "(namespace = 'user' AND owner_id IS NOT NULL)",
            name="ck_metric_definitions_namespace_owner",
        ),
        CheckConstraint("status IN ('active', 'retired')", name="ck_metric_definitions_status"),
    )


class MetricDefinitionVersion(Base):
    __tablename__ = "metric_definition_versions"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("metric_definitions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int]
    value_kind: Mapped[str]
    unit: Mapped[str | None]
    dimension: Mapped[str]
    scale_id: Mapped[str | None]
    scale_version: Mapped[int | None]
    aggregation: Mapped[str]
    coverage_policy: Mapped[dict] = mapped_column(JSONB)
    time_semantics: Mapped[str]
    minimum: Mapped[float | None]
    maximum: Mapped[float | None]
    labels: Mapped[dict] = mapped_column(JSONB)
    allowed_methods: Mapped[list] = mapped_column(JSONB)
    schema_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("definition_id", "version", name="uq_metric_definition_version"),
        CheckConstraint("version >= 1", name="ck_metric_definition_versions_version"),
        CheckConstraint(
            "value_kind IN ('physical_number', 'increment', 'interval_total', "
            "'cumulative_counter', 'ordinal', 'nominal', 'boolean')",
            name="ck_metric_definition_versions_kind",
        ),
        CheckConstraint(
            "time_semantics IN ('point', 'interval', 'calendar_period')",
            name="ck_metric_definition_versions_time",
        ),
    )


class EventMetricMapping(Base):
    __tablename__ = "event_metric_mappings"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    event_definition_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("event_definition_versions.id", ondelete="CASCADE"), index=True
    )
    field_id: Mapped[str]
    metric_definition_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("metric_definition_versions.id", ondelete="RESTRICT"), index=True
    )
    projection_version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "event_definition_version_id",
            "field_id",
            "projection_version",
            name="uq_event_metric_mapping_projection",
        ),
        CheckConstraint("projection_version >= 1", name="ck_event_metric_mapping_version"),
    )


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
    __table_args__ = (Index("ix_measurements_metric_quality_ts", "metric", "quality", "ts"),)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    metric: Mapped[str] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(primary_key=True, default="garmin_connect")
    local_date: Mapped[date] = mapped_column(index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str]
    metric_definition_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("metric_definition_versions.id", ondelete="RESTRICT"), index=True
    )
    source_ref: Mapped[uuid.UUID | None] = mapped_column(UUID, index=True)
    quality: Mapped[str] = mapped_column(default="observed")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class MetricObservation(Base):
    __tablename__ = "metric_observations"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    metric: Mapped[str]
    value: Mapped[float | None]
    value_text: Mapped[str | None]
    value_boolean: Mapped[bool | None]
    unit: Mapped[str]
    metric_definition_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("metric_definition_versions.id", ondelete="RESTRICT"), index=True
    )
    source_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="RESTRICT"), index=True
    )
    field_id: Mapped[str | None]
    projection_version: Mapped[int | None]
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_calendar_date: Mapped[date]
    source_ref: Mapped[uuid.UUID] = mapped_column(UUID)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    timezone: Mapped[str]
    account: Mapped[str | None]
    device: Mapped[str | None]
    quality: Mapped[str]
    precision: Mapped[float | None]
    coverage: Mapped[float | None]
    valid: Mapped[bool] = mapped_column(Boolean, default=True)
    sequence: Mapped[int]
    feature_version: Mapped[str]
    __table_args__ = (
        UniqueConstraint("source_ref", "fetched_at", "metric", "sequence", "feature_version"),
        UniqueConstraint(
            "source_entry_id", "field_id", "projection_version", name="uq_metric_projection_fact"
        ),
        Index("ix_metric_observations_asof", "metric", "observed_at", "ingested_at"),
        CheckConstraint(
            "(CASE WHEN value IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN value_text IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN value_boolean IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_metric_observations_typed_value",
        ),
        CheckConstraint(
            "coverage IS NULL OR (coverage >= 0 AND coverage <= 1)",
            name="ck_metric_observations_coverage",
        ),
    )


class HealthDay(Base):
    __tablename__ = "health_days"
    day: Mapped[date] = mapped_column(primary_key=True)
    hydration_ml: Mapped[float | None]
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
    definition_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_definition_versions.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(index=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str]
    source: Mapped[str]
    confidence: Mapped[float] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="confirmed")
    original_text: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    topology: Mapped[str] = mapped_column(default="point")
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
        CheckConstraint(
            "topology IN ('point', 'open_interval', 'bounded_interval', 'flexible')",
            name="ck_events_topology",
        ),
    )


class Audit(Base):
    __tablename__ = "audit_log"
    operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID, index=True)
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

Index(
    "ix_question_answer_retention",
    PendingQuestion.id,
    postgresql_where=PendingQuestion.evidence["answer_text"].astext.is_not(None),
)


Index(
    "ix_telegram_retention_age",
    TelegramUpdate.received_at,
    TelegramUpdate.id,
    postgresql_where=TelegramUpdate.status.in_(["processed", "invalid"])
    & TelegramUpdate.payload["_text_redacted"].astext.is_distinct_from("true"),
)
