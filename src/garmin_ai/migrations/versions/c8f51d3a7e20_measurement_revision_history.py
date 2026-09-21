"""Preserve immutable measurement revisions for historical cutoffs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c8f51d3a7e20"
down_revision = "b7c4e1a92d60"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "measurement_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("metric_definition_version_id", sa.UUID(), nullable=True),
        sa.Column("source_ref", sa.UUID(), nullable=True),
        sa.Column("quality", sa.String(), nullable=False, server_default="observed"),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["metric_definition_version_id"],
            ["metric_definition_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ts",
            "metric",
            "source",
            "source_ref",
            "ingested_at",
            name="uq_measurement_revision_source",
        ),
    )
    op.create_index(
        "ix_measurement_revisions_asof",
        "measurement_revisions",
        ["metric_definition_version_id", "ts", "ingested_at"],
    )
    op.create_index(
        op.f("ix_measurement_revisions_local_date"),
        "measurement_revisions",
        ["local_date"],
    )
    op.create_index(
        op.f("ix_measurement_revisions_metric"),
        "measurement_revisions",
        ["metric"],
    )
    op.create_index(
        op.f("ix_measurement_revisions_metric_definition_version_id"),
        "measurement_revisions",
        ["metric_definition_version_id"],
    )
    op.create_index(
        op.f("ix_measurement_revisions_source_ref"),
        "measurement_revisions",
        ["source_ref"],
    )
    op.create_index(
        op.f("ix_measurement_revisions_ts"),
        "measurement_revisions",
        ["ts"],
    )
    op.execute(
        """
        INSERT INTO measurement_revisions (
            id, ts, metric, source, local_date, value, unit,
            metric_definition_version_id, source_ref, quality, details, ingested_at
        )
        SELECT
            gen_random_uuid(), ts, metric, source, local_date, value, unit,
            metric_definition_version_id, source_ref, quality, details, ingested_at
        FROM measurements
        """
    )


def downgrade():
    op.drop_table("measurement_revisions")
