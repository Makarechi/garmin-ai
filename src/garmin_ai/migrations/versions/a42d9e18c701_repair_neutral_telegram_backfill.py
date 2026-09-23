"""Repair neutral Telegram history created by the original backfill."""

from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op

revision = "a42d9e18c701"
down_revision = "f103aa712b44"
branch_labels = None
depends_on = None

TELEGRAM_NAMESPACE = UUID("5ddd62fc-6890-44b6-86a2-20f77524378f")


def repair(bind):
    owners = (
        bind.execute(
            sa.text(
                """
                SELECT people.id AS owner_id,
                       COALESCE(
                           (SELECT external_id FROM channel_bindings
                            WHERE owner_id = people.id AND channel = 'telegram'
                            ORDER BY confirmed_at LIMIT 1),
                           (SELECT COALESCE(
                               payload #>> '{message,chat,id}',
                               payload #>> '{callback_query,message,chat,id}'
                            ) FROM telegram_updates ORDER BY received_at LIMIT 1),
                           'legacy-owner'
                       ) AS external_conversation_id,
                       md5('legacy:telegram:conversation:' || people.id::text)::uuid
                           AS old_conversation_id
                FROM people
                WHERE EXISTS (
                    SELECT 1 FROM conversations
                    WHERE owner_id = people.id AND channel = 'telegram'
                )
                """
            )
        )
        .mappings()
        .all()
    )
    for row in owners:
        external_id = str(row["external_conversation_id"])
        new_id = uuid5(
            TELEGRAM_NAMESPACE,
            f"{row['owner_id']}:telegram:primary:{external_id}",
        )
        old_id = row["old_conversation_id"]
        if new_id == old_id:
            continue
        bind.execute(
            sa.text(
                """
                INSERT INTO conversations (
                    id, owner_id, channel, channel_instance_id,
                    external_conversation_id, memory_epoch, state,
                    share_owner_memory, created_at, updated_at
                )
                SELECT :new_id, owner_id, channel, channel_instance_id,
                       :external_id, memory_epoch, state, share_owner_memory,
                       created_at, updated_at
                FROM conversations
                WHERE id = :old_id
                ON CONFLICT DO NOTHING
                """
            ),
            {"new_id": new_id, "old_id": old_id, "external_id": external_id},
        )
        for table in ("inbound_messages", "outbox_messages"):
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET conversation_id = :new_id WHERE conversation_id = :old_id"
                ),
                {"new_id": new_id, "old_id": old_id},
            )
        bind.execute(
            sa.text("DELETE FROM conversations WHERE id = :old_id"),
            {"old_id": old_id},
        )

    bind.execute(
        sa.text(
            """
            UPDATE inbound_messages AS inbound
            SET kind = 'voice',
                normalized_text = COALESCE(
                    updates.payload #>> '{message,text}',
                    updates.payload #>> '{message,caption}'
                ),
                envelope = inbound.envelope || jsonb_build_object(
                    'attachments', jsonb_build_array(jsonb_build_object(
                        'kind', 'voice',
                        'external_id', updates.payload #>> '{message,voice,file_id}',
                        'media_type', COALESCE(
                            updates.payload #>> '{message,voice,mime_type}', 'audio/ogg'
                        ),
                        'size_bytes', updates.payload #> '{message,voice,file_size}'
                    ))
                )
            FROM telegram_updates AS updates
            WHERE inbound.legacy_telegram_update_id = updates.id
              AND updates.payload #>> '{message,voice,file_id}' IS NOT NULL
            """
        )
    )


def upgrade():
    repair(op.get_bind())


def downgrade():
    # The old IDs and incomplete envelopes cannot be reconstructed without
    # disconnecting history from live ingress, so the data repair is retained.
    pass
