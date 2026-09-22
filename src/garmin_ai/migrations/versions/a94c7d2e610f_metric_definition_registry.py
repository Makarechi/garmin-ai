"""Add versioned metric contracts and reproducible event projections."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a94c7d2e610f"
down_revision = "f18d7c0b42a1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "metric_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(namespace = 'system' AND owner_id IS NULL) OR "
            "(namespace = 'user' AND owner_id IS NOT NULL)",
            name="ck_metric_definitions_namespace_owner",
        ),
        sa.CheckConstraint("status IN ('active', 'retired')", name="ck_metric_definitions_status"),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_metric_definitions_owner_id", "metric_definitions", ["owner_id"])
    op.create_table(
        "metric_definition_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("definition_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value_kind", sa.String(), nullable=False),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("dimension", sa.String(), nullable=False),
        sa.Column("scale_id", sa.String(), nullable=True),
        sa.Column("scale_version", sa.Integer(), nullable=True),
        sa.Column("aggregation", sa.String(), nullable=False),
        sa.Column("coverage_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("time_semantics", sa.String(), nullable=False),
        sa.Column("minimum", sa.Float(), nullable=True),
        sa.Column("maximum", sa.Float(), nullable=True),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("allowed_methods", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_hash", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "value_kind IN ('physical_number', 'increment', 'interval_total', "
            "'cumulative_counter', 'ordinal', 'nominal', 'boolean')",
            name="ck_metric_definition_versions_kind",
        ),
        sa.CheckConstraint(
            "time_semantics IN ('point', 'interval', 'calendar_period')",
            name="ck_metric_definition_versions_time",
        ),
        sa.CheckConstraint("version >= 1", name="ck_metric_definition_versions_version"),
        sa.ForeignKeyConstraint(["definition_id"], ["metric_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("definition_id", "version", name="uq_metric_definition_version"),
    )
    op.create_index(
        "ix_metric_definition_versions_definition_id",
        "metric_definition_versions",
        ["definition_id"],
    )
    op.create_table(
        "event_metric_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("field_id", sa.String(), nullable=False),
        sa.Column("metric_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("projection_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("projection_version >= 1", name="ck_event_metric_mapping_version"),
        sa.ForeignKeyConstraint(
            ["event_definition_version_id"],
            ["event_definition_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["metric_definition_version_id"],
            ["metric_definition_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_definition_version_id",
            "field_id",
            "projection_version",
            name="uq_event_metric_mapping_projection",
        ),
    )
    op.create_index(
        "ix_event_metric_mappings_event_definition_version_id",
        "event_metric_mappings",
        ["event_definition_version_id"],
    )
    op.create_index(
        "ix_event_metric_mappings_metric_definition_version_id",
        "event_metric_mappings",
        ["metric_definition_version_id"],
    )
    op.add_column(
        "measurements", sa.Column("metric_definition_version_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_measurements_metric_definition_version",
        "measurements",
        "metric_definition_versions",
        ["metric_definition_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_measurements_metric_definition_version_id",
        "measurements",
        ["metric_definition_version_id"],
    )
    op.alter_column("metric_observations", "value", existing_type=sa.Float(), nullable=True)
    for column in (
        sa.Column("value_text", sa.String(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("metric_definition_version_id", sa.Uuid(), nullable=True),
        sa.Column("source_entry_id", sa.Uuid(), nullable=True),
        sa.Column("field_id", sa.String(), nullable=True),
        sa.Column("projection_version", sa.Integer(), nullable=True),
        sa.Column("effective_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("precision", sa.Float(), nullable=True),
        sa.Column("coverage", sa.Float(), nullable=True),
        sa.Column("valid", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("metric_observations", column)
    op.create_foreign_key(
        "fk_metric_observations_definition_version",
        "metric_observations",
        "metric_definition_versions",
        ["metric_definition_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_metric_observations_source_entry",
        "metric_observations",
        "events",
        ["source_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_metric_observations_metric_definition_version_id",
        "metric_observations",
        ["metric_definition_version_id"],
    )
    op.create_index(
        "ix_metric_observations_source_entry_id",
        "metric_observations",
        ["source_entry_id"],
    )
    op.create_unique_constraint(
        "uq_metric_projection_fact",
        "metric_observations",
        ["source_entry_id", "field_id", "projection_version"],
    )
    op.create_check_constraint(
        "ck_metric_observations_typed_value",
        "metric_observations",
        "(CASE WHEN value IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN value_text IS NULL THEN 0 ELSE 1 END + "
        "CASE WHEN value_boolean IS NULL THEN 0 ELSE 1 END) = 1",
    )
    op.create_check_constraint(
        "ck_metric_observations_coverage",
        "metric_observations",
        "coverage IS NULL OR (coverage >= 0 AND coverage <= 1)",
    )
    op.execute(
        """
        CREATE FUNCTION reject_metric_definition_version_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'metric definition versions are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER metric_definition_versions_immutable
        BEFORE UPDATE ON metric_definition_versions
        FOR EACH ROW EXECUTE FUNCTION reject_metric_definition_version_update()
        """
    )


def downgrade():
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM metric_observations WHERE value IS NULL) THEN
                RAISE EXCEPTION 'cannot downgrade while categorical metric observations exist';
            END IF;
        END $$
        """
    )
    op.execute("DROP TRIGGER metric_definition_versions_immutable ON metric_definition_versions")
    op.execute("DROP FUNCTION reject_metric_definition_version_update()")
    op.drop_constraint("ck_metric_observations_coverage", "metric_observations", type_="check")
    op.drop_constraint("ck_metric_observations_typed_value", "metric_observations", type_="check")
    op.drop_constraint("uq_metric_projection_fact", "metric_observations", type_="unique")
    op.drop_index("ix_metric_observations_source_entry_id", table_name="metric_observations")
    op.drop_index(
        "ix_metric_observations_metric_definition_version_id",
        table_name="metric_observations",
    )
    op.drop_constraint(
        "fk_metric_observations_source_entry", "metric_observations", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_metric_observations_definition_version",
        "metric_observations",
        type_="foreignkey",
    )
    for name in (
        "valid",
        "coverage",
        "invalidated_at",
        "precision",
        "uploaded_at",
        "recorded_at",
        "effective_end",
        "projection_version",
        "field_id",
        "source_entry_id",
        "metric_definition_version_id",
        "value_boolean",
        "value_text",
    ):
        op.drop_column("metric_observations", name)
    op.alter_column("metric_observations", "value", existing_type=sa.Float(), nullable=False)
    op.drop_index("ix_measurements_metric_definition_version_id", table_name="measurements")
    op.drop_constraint(
        "fk_measurements_metric_definition_version", "measurements", type_="foreignkey"
    )
    op.drop_column("measurements", "metric_definition_version_id")
    op.drop_index(
        "ix_event_metric_mappings_metric_definition_version_id",
        table_name="event_metric_mappings",
    )
    op.drop_index(
        "ix_event_metric_mappings_event_definition_version_id",
        table_name="event_metric_mappings",
    )
    op.drop_table("event_metric_mappings")
    op.drop_index(
        "ix_metric_definition_versions_definition_id",
        table_name="metric_definition_versions",
    )
    op.drop_table("metric_definition_versions")
    op.drop_index("ix_metric_definitions_owner_id", table_name="metric_definitions")
    op.drop_table("metric_definitions")
