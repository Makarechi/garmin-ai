"""Index the unredacted transport retention cursor."""

import sqlalchemy as sa
from alembic import op

revision = "c42f8910e615"
down_revision = "b91d02a4c703"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_telegram_retention_age",
        "telegram_updates",
        ["received_at", "id"],
        postgresql_where=sa.text(
            "status = 'processed' AND (payload ->> '_text_redacted') IS DISTINCT FROM 'true'"
        ),
    )


def downgrade():
    op.drop_index("ix_telegram_retention_age", table_name="telegram_updates")
