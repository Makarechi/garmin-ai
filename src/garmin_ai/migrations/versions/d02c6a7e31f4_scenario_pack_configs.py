"""Add independent owner configuration for scenario packs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d02c6a7e31f4"
down_revision = "c71a5e4d290b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "module_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("pack_key", sa.String(), nullable=False),
        sa.Column("tracking_enabled", sa.Boolean(), nullable=False),
        sa.Column("collection_enabled", sa.Boolean(), nullable=False),
        sa.Column("reminders_enabled", sa.Boolean(), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("llm_enabled", sa.Boolean(), nullable=False),
        sa.Column("outcome_goal", sa.Text(), nullable=True),
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
        sa.CheckConstraint("revision >= 1", name="ck_module_configs_revision"),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "pack_key", name="uq_owner_module_pack"),
    )
    op.create_index("ix_module_configs_owner_id", "module_configs", ["owner_id"])


def downgrade():
    op.drop_index("ix_module_configs_owner_id", table_name="module_configs")
    op.drop_table("module_configs")
