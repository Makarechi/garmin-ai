"""Bound proactive answer retention with an indexed independent cursor."""

import sqlalchemy as sa
from alembic import op

revision = "d31e572abc90"
down_revision = "c42f8910e615"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_question_answer_retention",
        "pending_questions",
        ["id"],
        postgresql_where=sa.text("(evidence ->> 'answer_text') IS NOT NULL"),
    )


def downgrade():
    op.drop_index("ix_question_answer_retention", table_name="pending_questions")
