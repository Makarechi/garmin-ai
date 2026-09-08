"""canonical storage and durable job queue

Revision ID: bfccd06bf1c6
Revises:
Create Date: 2026-09-07 23:46:12.948313

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "bfccd06bf1c6"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.create_table(
        "activities",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("moving_seconds", sa.Float(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("avg_hr", sa.Float(), nullable=True),
        sa.Column("max_hr", sa.Float(), nullable=True),
        sa.Column("avg_speed_mps", sa.Float(), nullable=True),
        sa.Column("calories", sa.Float(), nullable=True),
        sa.Column("cadence", sa.Float(), nullable=True),
        sa.Column("ascent_m", sa.Float(), nullable=True),
        sa.Column("descent_m", sa.Float(), nullable=True),
        sa.Column("aerobic_effect", sa.Float(), nullable=True),
        sa.Column("anaerobic_effect", sa.Float(), nullable=True),
        sa.Column("training_load", sa.Float(), nullable=True),
        sa.Column("fit_key", sa.String(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint('"end" >= start'),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_activities_start"), "activities", ["start"], unique=False)
    op.create_table(
        "app_state",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_event_id"), "audit_log", ["event_id"], unique=False)
    op.create_table(
        "events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint('"end" IS NULL OR "end" >= start'),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(op.f("ix_events_kind"), "events", ["kind"], unique=False)
    op.create_index(op.f("ix_events_start"), "events", ["start"], unique=False)
    op.create_table(
        "health_days",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("sleep_score", sa.Float(), nullable=True),
        sa.Column("sleep_seconds", sa.Float(), nullable=True),
        sa.Column("deep_seconds", sa.Float(), nullable=True),
        sa.Column("rem_seconds", sa.Float(), nullable=True),
        sa.Column("light_seconds", sa.Float(), nullable=True),
        sa.Column("awake_seconds", sa.Float(), nullable=True),
        sa.Column("hrv_nightly_avg", sa.Float(), nullable=True),
        sa.Column("hrv_weekly_avg", sa.Float(), nullable=True),
        sa.Column("hrv_baseline_low", sa.Float(), nullable=True),
        sa.Column("hrv_baseline_high", sa.Float(), nullable=True),
        sa.Column("hrv_status", sa.String(), nullable=True),
        sa.Column("resting_hr", sa.Float(), nullable=True),
        sa.Column("body_battery_high", sa.Float(), nullable=True),
        sa.Column("body_battery_low", sa.Float(), nullable=True),
        sa.Column("body_battery_charged", sa.Float(), nullable=True),
        sa.Column("body_battery_drained", sa.Float(), nullable=True),
        sa.Column("stress_avg", sa.Float(), nullable=True),
        sa.Column("stress_max", sa.Float(), nullable=True),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("active_calories", sa.Float(), nullable=True),
        sa.Column("intensity_minutes", sa.Float(), nullable=True),
        sa.Column("training_readiness_score", sa.Float(), nullable=True),
        sa.Column("recovery_time_minutes", sa.Float(), nullable=True),
        sa.Column("training_status", sa.String(), nullable=True),
        sa.Column("vo2max", sa.Float(), nullable=True),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("day"),
    )
    op.create_table(
        "insights",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("statement", sa.String(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("effect_size", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("dedup_key", sa.String(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedup_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key"),
    )
    op.create_index("ix_job_due", "jobs", ["status", "run_at"], unique=False)
    op.create_index(op.f("ix_jobs_run_at"), "jobs", ["run_at"], unique=False)
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)
    op.create_table(
        "measurements",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("source_ref", sa.UUID(), nullable=True),
        sa.Column("quality", sa.String(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("ts", "metric", "source"),
    )
    op.create_index(
        op.f("ix_measurements_local_date"), "measurements", ["local_date"], unique=False
    )
    op.create_table(
        "pending_questions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("priority", sa.Float(), nullable=False),
        sa.Column("earliest_send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("dedup_key", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key"),
    )
    op.create_table(
        "source_payloads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("archive_key", sa.String(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parser_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "endpoint", "source_key", "payload_hash"),
    )
    op.create_table(
        "telegram_updates",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "timeline_intervals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_timeline_intervals_start"), "timeline_intervals", ["start"], unique=False
    )
    op.create_table(
        "activity_parts",
        sa.Column("activity_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("activity_id", "kind", "sequence"),
    )
    # ### end Alembic commands ###
    op.execute("SELECT create_hypertable('measurements', 'ts', if_not_exists => TRUE)")


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("activity_parts")
    op.drop_index(op.f("ix_timeline_intervals_start"), table_name="timeline_intervals")
    op.drop_table("timeline_intervals")
    op.drop_table("telegram_updates")
    op.drop_table("source_payloads")
    op.drop_table("pending_questions")
    op.drop_index(op.f("ix_measurements_local_date"), table_name="measurements")
    op.drop_table("measurements")
    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_run_at"), table_name="jobs")
    op.drop_index("ix_job_due", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("insights")
    op.drop_table("health_days")
    op.drop_index(op.f("ix_events_start"), table_name="events")
    op.drop_index(op.f("ix_events_kind"), table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_audit_log_event_id"), table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("app_state")
    op.drop_index(op.f("ix_activities_start"), table_name="activities")
    op.drop_table("activities")
    # ### end Alembic commands ###
