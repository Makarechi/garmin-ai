"""Repair legacy Telegram conversation IDs after neutral-message migration."""

import hashlib
import json
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op

revision = "a72d9f4c8e31"
down_revision = "a42d9e18c701"
branch_labels = None
depends_on = None

TELEGRAM_NAMESPACE = UUID("5ddd62fc-6890-44b6-86a2-20f77524378f")


def _move(bind, row, target_id):
    if row["id"] == target_id:
        return
    existing = (
        bind.execute(
            sa.text(
                "SELECT owner_id, channel, channel_instance_id, external_conversation_id "
                "FROM conversations WHERE id = :target"
            ),
            {"target": target_id},
        )
        .mappings()
        .one_or_none()
    )
    if existing is None:
        bind.execute(
            sa.text(
                """
                INSERT INTO conversations (
                    id, owner_id, channel, channel_instance_id, external_conversation_id,
                    memory_epoch, state, share_owner_memory, created_at, updated_at
                ) VALUES (
                    :target, :owner_id, :channel, :channel_instance_id, NULL,
                    :memory_epoch, CAST(:state AS jsonb), :share_owner_memory, :created_at,
                    :updated_at
                )
                """
            ),
            {**row, "state": json.dumps(row["state"]), "target": target_id},
        )
    elif (
        existing["owner_id"],
        existing["channel"],
        existing["channel_instance_id"],
    ) != (row["owner_id"], row["channel"], row["channel_instance_id"]):
        raise ValueError("Telegram conversation ID repair found an identity collision")
    for table in ("inbound_messages", "outbox_messages"):
        bind.execute(
            sa.text(f"UPDATE {table} SET conversation_id = :target WHERE conversation_id = :old"),
            {"target": target_id, "old": row["id"]},
        )
    bind.execute(sa.text("DELETE FROM conversations WHERE id = :old"), {"old": row["id"]})
    bind.execute(
        sa.text("UPDATE conversations SET external_conversation_id = :external WHERE id = :target"),
        {"external": row["external_conversation_id"], "target": target_id},
    )


def _telegram_rows(bind):
    return (
        bind.execute(
            sa.text(
                """
            SELECT id, owner_id, channel, channel_instance_id, external_conversation_id,
                   memory_epoch, state, share_owner_memory, created_at, updated_at
            FROM conversations
            WHERE channel = 'telegram' AND external_conversation_id IS NOT NULL
            ORDER BY created_at, id
            """
            )
        )
        .mappings()
        .all()
    )


def upgrade():
    bind = op.get_bind()
    for row in _telegram_rows(bind):
        target = uuid5(
            TELEGRAM_NAMESPACE,
            f"{row['owner_id']}:{row['channel']}:{row['channel_instance_id']}:"
            f"{row['external_conversation_id']}",
        )
        _move(bind, dict(row), target)


def downgrade():
    bind = op.get_bind()
    for row in _telegram_rows(bind):
        legacy = UUID(
            hashlib.md5(
                f"legacy:telegram:conversation:{row['owner_id']}".encode(),
                usedforsecurity=False,
            ).hexdigest()
        )
        _move(bind, dict(row), legacy)
