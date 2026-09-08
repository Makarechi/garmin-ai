"""Index source references for bounded corrected-sample replacement."""

from alembic import op

revision = "4c9e28f110ab"
down_revision = "bfccd06bf1c6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_measurements_source_ref", "measurements", ["source_ref"])


def downgrade():
    op.drop_index("ix_measurements_source_ref", table_name="measurements")
