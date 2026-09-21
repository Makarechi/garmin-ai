"""Preserve nominal domains and measurement ingestion time."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b7c4e1a92d60"
down_revision = "f103aa712b44"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "metric_definition_versions",
        sa.Column("category_domain", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "measurements",
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE measurements AS measurement
        SET ingested_at = COALESCE(
            (
                SELECT payload.fetched_at
                FROM source_payloads AS payload
                WHERE payload.id = measurement.source_ref
            ),
            CURRENT_TIMESTAMP
        )
        """
    )
    op.alter_column("measurements", "ingested_at", nullable=False)


def downgrade():
    op.drop_column("measurements", "ingested_at")
    op.drop_column("metric_definition_versions", "category_domain")
