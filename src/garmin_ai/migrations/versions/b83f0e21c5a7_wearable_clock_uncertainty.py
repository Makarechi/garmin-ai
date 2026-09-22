"""Retain wearable clock uncertainty in the canonical event envelope."""

import sqlalchemy as sa
from alembic import op

revision = "b83f0e21c5a7"
down_revision = "e13b7c8f42a0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("clock_uncertainty_seconds", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_events_clock_uncertainty",
        "events",
        "clock_uncertainty_seconds IS NULL OR clock_uncertainty_seconds BETWEEN 0 AND 31536000",
    )


def downgrade():
    op.drop_constraint("ck_events_clock_uncertainty", "events", type_="check")
    op.drop_column("events", "clock_uncertainty_seconds")
