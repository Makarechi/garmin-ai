"""Add trusted provenance and time metadata to the canonical event envelope."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c71a5e4d290b"
down_revision = "a94c7d2e610f"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM events
                WHERE source NOT IN (
                    'manual', 'telegram_text', 'telegram_button', 'telegram_voice',
                    'mcp', 'inferred', 'wearable'
                )
            ) THEN
                RAISE EXCEPTION 'cannot backfill provenance for an unknown legacy source';
            END IF;
        END $$
        """
    )
    columns = (
        sa.Column("envelope_version", sa.Integer(), nullable=True),
        sa.Column("time_precision", sa.String(), nullable=True),
        sa.Column("assertion_kind", sa.String(), nullable=True),
        sa.Column("producer", sa.String(), nullable=True),
        sa.Column("transport", sa.String(), nullable=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validation_status", sa.String(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in columns:
        op.add_column("events", column)
    op.execute(
        """
        UPDATE events SET
            envelope_version = 1,
            time_precision = CASE
                WHEN topology = 'point' THEN 'instant'
                WHEN topology IN ('bounded_interval', 'open_interval') THEN 'interval'
                ELSE 'unknown'
            END,
            assertion_kind = CASE
                WHEN source = 'wearable' AND EXISTS (
                    SELECT 1 FROM audit_log a
                    WHERE a.event_id = events.id AND a.action = 'create'
                      AND a.actor LIKE 'wearable:%'
                ) THEN 'device_measurement'
                WHEN source = 'inferred' THEN 'inferred'
                ELSE 'user_report'
            END,
            producer = CASE
                WHEN source LIKE 'telegram_%' THEN 'telegram'
                WHEN source = 'wearable' AND EXISTS (
                    SELECT 1 FROM audit_log a
                    WHERE a.event_id = events.id AND a.action = 'create'
                      AND a.actor LIKE 'wearable:%'
                ) THEN 'wearable'
                WHEN source = 'inferred' THEN 'system'
                ELSE 'owner'
            END,
            transport = CASE
                WHEN source LIKE 'telegram_%' OR source IN ('manual', 'mcp') THEN source
                WHEN source = 'wearable' AND EXISTS (
                    SELECT 1 FROM audit_log a
                    WHERE a.event_id = events.id AND a.action = 'create'
                      AND a.actor LIKE 'wearable:%'
                ) THEN 'connector'
                ELSE NULL
            END,
            author = CASE
                WHEN source IN (
                    'manual', 'telegram_text', 'telegram_button', 'telegram_voice', 'mcp'
                ) THEN 'owner'
                ELSE NULL
            END,
            evidence_refs = '[]'::jsonb,
            validation_status = CASE
                WHEN status IN ('inferred', 'needs_confirmation') THEN 'needs_confirmation'
                WHEN source = 'wearable' AND EXISTS (
                    SELECT 1 FROM audit_log a
                    WHERE a.event_id = events.id AND a.action = 'create'
                      AND a.actor LIKE 'wearable:%'
                ) THEN 'trusted'
                ELSE 'schema_validated'
            END,
            recorded_at = created_at,
            ingested_at = created_at
        """
    )
    defaults = {
        "envelope_version": "1",
        "time_precision": "'instant'",
        "assertion_kind": "'user_report'",
        "producer": "'owner'",
        "evidence_refs": "'[]'::jsonb",
        "validation_status": "'schema_validated'",
        "recorded_at": "now()",
        "ingested_at": "now()",
    }
    for name in (
        "envelope_version",
        "time_precision",
        "assertion_kind",
        "producer",
        "evidence_refs",
        "validation_status",
        "recorded_at",
        "ingested_at",
    ):
        op.alter_column("events", name, nullable=False, server_default=sa.text(defaults[name]))
    op.create_check_constraint("ck_events_envelope_version", "events", "envelope_version = 1")
    op.create_check_constraint(
        "ck_events_time_precision",
        "events",
        "time_precision IN ('instant', 'interval', 'calendar_date', 'unknown')",
    )
    op.create_check_constraint(
        "ck_events_assertion_kind",
        "events",
        "assertion_kind IN ('user_report', 'device_measurement', 'derived', 'inferred')",
    )
    op.create_check_constraint(
        "ck_events_validation_status",
        "events",
        "validation_status IN ('trusted', 'schema_validated', 'needs_confirmation')",
    )
    op.create_check_constraint(
        "ck_events_evidence_refs_array",
        "events",
        "jsonb_typeof(evidence_refs) = 'array'",
    )


def downgrade():
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1
                FROM events e
                JOIN event_definition_versions v ON v.id = e.definition_version_id
                JOIN event_definitions d ON d.id = v.definition_id
                WHERE d.namespace = 'user'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade canonical envelopes while custom entries exist; restore or roll forward';
            END IF;
            IF EXISTS (
                SELECT 1 FROM events e WHERE
                    e.envelope_version IS DISTINCT FROM 1
                    OR e.time_precision IS DISTINCT FROM CASE
                        WHEN e.topology = 'point' THEN 'instant'
                        WHEN e.topology IN ('bounded_interval', 'open_interval') THEN 'interval'
                        ELSE 'unknown' END
                    OR e.assertion_kind IS DISTINCT FROM CASE
                        WHEN e.source = 'wearable' THEN 'device_measurement'
                        WHEN e.source = 'inferred' THEN 'inferred'
                        ELSE 'user_report' END
                    OR e.producer IS DISTINCT FROM CASE
                        WHEN e.source LIKE 'telegram_%' THEN 'telegram'
                        WHEN e.source = 'wearable' THEN 'wearable'
                        WHEN e.source = 'inferred' THEN 'system'
                        ELSE 'owner' END
                    OR e.transport IS DISTINCT FROM CASE
                        WHEN e.source LIKE 'telegram_%' OR e.source IN ('manual', 'mcp')
                            THEN e.source
                        WHEN e.source = 'wearable' THEN 'connector'
                        ELSE NULL END
                    OR e.author IS DISTINCT FROM CASE
                        WHEN e.source IN (
                            'manual', 'telegram_text', 'telegram_button', 'telegram_voice', 'mcp'
                        ) THEN 'owner' ELSE NULL END
                    OR e.evidence_refs IS DISTINCT FROM '[]'::jsonb
                    OR e.validation_status IS DISTINCT FROM CASE
                        WHEN e.status IN ('inferred', 'needs_confirmation')
                            THEN 'needs_confirmation'
                        WHEN e.source = 'wearable' THEN 'trusted'
                        ELSE 'schema_validated' END
                    OR e.recorded_at IS DISTINCT FROM e.created_at
                    OR e.ingested_at IS DISTINCT FROM e.created_at
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade canonical envelopes with non-legacy provenance; restore or roll forward';
            END IF;
        END $$
        """
    )
    for name in (
        "ck_events_evidence_refs_array",
        "ck_events_validation_status",
        "ck_events_assertion_kind",
        "ck_events_time_precision",
        "ck_events_envelope_version",
    ):
        op.drop_constraint(name, "events", type_="check")
    for name in (
        "ingested_at",
        "recorded_at",
        "validation_status",
        "evidence_refs",
        "author",
        "transport",
        "producer",
        "assertion_kind",
        "time_precision",
        "envelope_version",
    ):
        op.drop_column("events", name)
