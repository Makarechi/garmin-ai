"""Group atomic diary creation batches for operation-level undo."""

import sqlalchemy as sa
from alembic import op

revision = "b91d02a4c703"
down_revision = "a637902bf114"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("audit_log", sa.Column("operation_id", sa.Uuid(), nullable=True))
    op.create_index("ix_audit_log_operation_id", "audit_log", ["operation_id"])


def downgrade():
    op.drop_index("ix_audit_log_operation_id", table_name="audit_log")
    op.drop_column("audit_log", "operation_id")
