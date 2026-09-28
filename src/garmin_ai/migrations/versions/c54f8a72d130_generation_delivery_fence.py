"""Persist memory consent fences for generated outbox messages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c54f8a72d130"
down_revision = "c8f51d3a7e20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox_messages", sa.Column("memory_fence", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox_messages", "memory_fence")
