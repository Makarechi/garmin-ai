"""Keep daily hydration totals out of the intraday stream."""

import sqlalchemy as sa
from alembic import op

revision = "95b14d720a3f"
down_revision = "84a03c619f2e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("health_days", sa.Column("hydration_ml", sa.Float()))
    # Preserve raw provenance; old midnight measurements remain stored but the
    # catalog excludes them from intraday analyses. Replay populates daily totals.


def downgrade():
    op.drop_column("health_days", "hydration_ml")
