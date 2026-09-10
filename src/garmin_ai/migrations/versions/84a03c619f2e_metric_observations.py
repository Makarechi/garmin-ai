"""Preserve timestamped metric versions independently of daily projections."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "84a03c619f2e"
down_revision = "4c9e28f110ab"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "metric_observations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("effective_start", sa.DateTime(timezone=True)),
        sa.Column("source_calendar_date", sa.Date(), nullable=False),
        sa.Column("source_ref", UUID, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("account", sa.String()),
        sa.Column("device", sa.String()),
        sa.Column("quality", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("feature_version", sa.String(), nullable=False),
        sa.UniqueConstraint("source_ref", "fetched_at", "metric", "sequence", "feature_version"),
    )
    op.create_index(
        "ix_metric_observations_asof",
        "metric_observations",
        ["metric", "observed_at", "ingested_at"],
    )


def downgrade():
    op.drop_table("metric_observations")
