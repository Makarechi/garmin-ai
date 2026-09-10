"""Index channel-specific observation recency across retained history."""

from alembic import op

revision = "a637902bf114"
down_revision = "95b14d720a3f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_measurements_metric_quality_ts", "measurements", ["metric", "quality", "ts"]
    )


def downgrade():
    op.drop_index("ix_measurements_metric_quality_ts", table_name="measurements")
