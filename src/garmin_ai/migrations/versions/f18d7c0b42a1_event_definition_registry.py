"""Add versioned event definitions and bind events to immutable versions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f18d7c0b42a1"
down_revision = "e6b8f0a13c72"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "event_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=True),
        sa.Column("draft", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(namespace = 'system' AND owner_id IS NULL) OR "
            "(namespace = 'user' AND owner_id IS NOT NULL)",
            name="ck_event_definitions_namespace_owner",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_event_definitions_revision"),
        sa.CheckConstraint(
            "status IN ('draft', 'proposed', 'active', 'retired')",
            name="ck_event_definitions_status",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_event_definitions_owner_id", "event_definitions", ["owner_id"])
    op.create_table(
        "event_definition_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("definition_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_hash", sa.String(), nullable=False),
        sa.Column("topology", sa.String(), nullable=False),
        sa.Column("field_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("privacy", sa.String(), nullable=False),
        sa.Column("allowed_operations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "privacy IN ('private', 'sensitive')",
            name="ck_event_definition_versions_privacy",
        ),
        sa.CheckConstraint(
            "topology IN ('point', 'open_interval', 'bounded_interval', 'flexible')",
            name="ck_event_definition_versions_topology",
        ),
        sa.CheckConstraint("version >= 1", name="ck_event_definition_versions_version"),
        sa.ForeignKeyConstraint(["definition_id"], ["event_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("definition_id", "version", name="uq_event_definition_version"),
    )
    op.create_index(
        "ix_event_definition_versions_definition_id",
        "event_definition_versions",
        ["definition_id"],
    )
    op.add_column("events", sa.Column("definition_version_id", sa.Uuid(), nullable=True))
    op.add_column(
        "events", sa.Column("topology", sa.String(), server_default="point", nullable=False)
    )
    op.execute(
        """
        UPDATE events
        SET topology = CASE
            WHEN "end" IS NULL AND kind IN ('migraine', 'illness') THEN 'open_interval'
            WHEN "end" IS NULL OR "end" = start THEN 'point'
            ELSE 'bounded_interval'
        END
        """
    )
    op.create_check_constraint(
        "ck_events_topology",
        "events",
        "topology IN ('point', 'open_interval', 'bounded_interval', 'flexible')",
    )
    op.create_foreign_key(
        "fk_events_definition_version_id",
        "events",
        "event_definition_versions",
        ["definition_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_events_definition_version_id", "events", ["definition_version_id"])
    op.execute(
        """
        CREATE FUNCTION reject_event_definition_version_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'event definition versions are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER event_definition_versions_immutable
        BEFORE UPDATE ON event_definition_versions
        FOR EACH ROW EXECUTE FUNCTION reject_event_definition_version_update()
        """
    )


def downgrade():
    op.execute("DROP TRIGGER event_definition_versions_immutable ON event_definition_versions")
    op.execute("DROP FUNCTION reject_event_definition_version_update()")
    op.drop_index("ix_events_definition_version_id", table_name="events")
    op.drop_constraint("fk_events_definition_version_id", "events", type_="foreignkey")
    op.drop_constraint("ck_events_topology", "events", type_="check")
    op.drop_column("events", "topology")
    op.drop_column("events", "definition_version_id")
    op.drop_index(
        "ix_event_definition_versions_definition_id",
        table_name="event_definition_versions",
    )
    op.drop_table("event_definition_versions")
    op.drop_index("ix_event_definitions_owner_id", table_name="event_definitions")
    op.drop_table("event_definitions")
