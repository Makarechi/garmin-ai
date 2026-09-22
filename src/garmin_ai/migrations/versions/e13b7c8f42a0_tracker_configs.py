"""Add owner configuration for generated trackers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e13b7c8f42a0"
down_revision = "e6f24a9b31d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tracker_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("definition_id", sa.Uuid(), nullable=False),
        sa.Column("shortcut", sa.String(), nullable=True),
        sa.Column("reminder_enabled", sa.Boolean(), nullable=False),
        sa.Column("reminder_time", sa.String(), nullable=True),
        sa.Column("reminder_timezone", sa.String(), nullable=True),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision >= 1", name="ck_tracker_configs_revision"),
        sa.CheckConstraint(
            "reminder_time IS NULL OR reminder_time ~ '^(?:[01][0-9]|2[0-3]):[0-5][0-9]$'",
            name="ck_tracker_configs_reminder_time",
        ),
        sa.ForeignKeyConstraint(["definition_id"], ["event_definitions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("definition_id"),
    )
    op.create_index("ix_tracker_configs_definition_id", "tracker_configs", ["definition_id"])
    op.create_index("ix_tracker_configs_owner_id", "tracker_configs", ["owner_id"])


def downgrade():
    op.drop_index("ix_tracker_configs_owner_id", table_name="tracker_configs")
    op.drop_index("ix_tracker_configs_definition_id", table_name="tracker_configs")
    op.drop_table("tracker_configs")
