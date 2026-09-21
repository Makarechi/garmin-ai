"""Retain replaced Garmin measurements for as-known metric queries."""

import sqlalchemy as sa
from alembic import op

revision = "e6f24a9b31d0"
down_revision = "d02c6a7e31f4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "measurement_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column(
            "metric_definition_version_id",
            sa.Uuid(),
            sa.ForeignKey("metric_definition_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column("source_ref", sa.Uuid(), nullable=False),
        sa.Column("quality", sa.String(), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_measurement_history_metric_definition_version_id",
        "measurement_history",
        ["metric_definition_version_id"],
    )
    op.create_index(
        "ix_measurement_history_asof",
        "measurement_history",
        ["metric_definition_version_id", "ts", "known_at"],
    )


def downgrade():
    op.drop_index("ix_measurement_history_asof", table_name="measurement_history")
    op.drop_index(
        "ix_measurement_history_metric_definition_version_id", table_name="measurement_history"
    )
    op.drop_table("measurement_history")
