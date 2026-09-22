"""Add channel-neutral conversations, inbox, outbox, and delivery evidence."""

from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f103aa712b44"
down_revision = "b83f0e21c5a7"
branch_labels = None
depends_on = None
TELEGRAM_NAMESPACE = UUID("5ddd62fc-6890-44b6-86a2-20f77524378f")


def upgrade():
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("channel_instance_id", sa.String(), nullable=False),
        sa.Column("external_conversation_id", sa.String(), nullable=True),
        sa.Column("memory_epoch", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("share_owner_memory", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "channel",
            "channel_instance_id",
            "external_conversation_id",
            name="uq_conversation_external_identity",
        ),
    )
    op.create_index("ix_conversations_owner_id", "conversations", ["owner_id"])
    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("channel_instance_id", sa.String(), nullable=False),
        sa.Column("external_event_id", sa.String(), nullable=False),
        sa.Column("external_message_id", sa.String(), nullable=True),
        sa.Column("sender_ref", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("legacy_telegram_update_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("revision >= 1", name="ck_inbound_revision_positive"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["inbound_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_telegram_update_id"),
        sa.UniqueConstraint(
            "channel",
            "channel_instance_id",
            "external_event_id",
            "revision",
            name="uq_inbound_transport_revision",
        ),
    )
    op.create_index("ix_inbound_messages_owner_id", "inbound_messages", ["owner_id"])
    op.create_index("ix_inbound_messages_conversation_id", "inbound_messages", ["conversation_id"])
    op.create_index("ix_inbound_messages_operation_id", "inbound_messages", ["operation_id"])
    op.create_index("ix_inbound_messages_status", "inbound_messages", ["status"])
    op.create_index(
        "ix_inbound_retention_age",
        "inbound_messages",
        ["received_at", "id"],
        postgresql_where=sa.text(
            "status IN ('processed', 'invalid') "
            "AND (envelope ->> '_text_redacted') IS DISTINCT FROM 'true'"
        ),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("inbound_message_id", sa.Uuid(), nullable=True),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("intent", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedup_key", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider_reference", sa.String(), nullable=True),
        sa.Column("legacy_key", sa.String(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["inbound_message_id"], ["inbound_messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key"),
        sa.UniqueConstraint("legacy_key"),
    )
    op.create_index("ix_outbox_messages_owner_id", "outbox_messages", ["owner_id"])
    op.create_index("ix_outbox_messages_conversation_id", "outbox_messages", ["conversation_id"])
    op.create_index(
        "ix_outbox_messages_inbound_message_id", "outbox_messages", ["inbound_message_id"]
    )
    op.create_index("ix_outbox_messages_operation_id", "outbox_messages", ["operation_id"])
    op.create_index("ix_outbox_messages_state", "outbox_messages", ["state"])
    op.create_table(
        "message_delivery_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("outbox_message_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_reference", sa.String(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["outbox_message_id"], ["outbox_messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "outbox_message_id", "state", "observed_at", name="uq_delivery_receipt_evidence"
        ),
    )
    op.create_index(
        "ix_message_delivery_receipts_outbox_message_id",
        "message_delivery_receipts",
        ["outbox_message_id"],
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "CREATE TEMP TABLE legacy_telegram_conversation_ids ("
            "owner_id uuid PRIMARY KEY, conversation_id uuid NOT NULL, "
            "external_conversation_id text NOT NULL) ON COMMIT DROP"
        )
    )
    legacy_owners = (
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
                   ) AS external_conversation_id
            FROM people
            """
            )
        )
        .mappings()
        .all()
    )
    for row in legacy_owners:
        external_id = str(row["external_conversation_id"])
        bind.execute(
            sa.text(
                "INSERT INTO legacy_telegram_conversation_ids "
                "(owner_id, conversation_id, external_conversation_id) "
                "VALUES (:owner_id, :conversation_id, :external_id)"
            ),
            {
                "owner_id": row["owner_id"],
                "conversation_id": uuid5(
                    TELEGRAM_NAMESPACE,
                    f"{row['owner_id']}:telegram:primary:{external_id}",
                ),
                "external_id": external_id,
            },
        )

    op.execute(
        """
        INSERT INTO conversations (
            id, owner_id, channel, channel_instance_id, external_conversation_id,
            memory_epoch, state, share_owner_memory
        )
        SELECT
            legacy.conversation_id,
            legacy.owner_id,
            'telegram',
            'primary',
            legacy.external_conversation_id,
            md5('legacy:telegram:epoch:' || legacy.owner_id::text)::uuid,
            '{}'::jsonb,
            FALSE
        FROM legacy_telegram_conversation_ids AS legacy
        """
    )
    op.execute(
        """
        INSERT INTO inbound_messages (
            id, owner_id, conversation_id, channel, channel_instance_id,
            external_event_id, external_message_id, sender_ref, occurred_at,
            received_at, kind, normalized_text, envelope, revision, status,
            operation_id, legacy_telegram_update_id
        )
        SELECT
            md5('legacy:telegram:update:' || updates.id::text)::uuid,
            people.id,
            legacy.conversation_id,
            'telegram',
            'primary',
            updates.id::text,
            COALESCE(
                updates.payload #>> '{message,message_id}',
                updates.payload #>> '{callback_query,message,message_id}'
            ),
            COALESCE(
                updates.payload #>> '{message,from,id}',
                updates.payload #>> '{callback_query,from,id}',
                'legacy-owner'
            ),
            CASE WHEN COALESCE(
                updates.payload #>> '{message,date}',
                updates.payload #>> '{callback_query,message,date}'
            ) ~ '^[0-9]+$' THEN to_timestamp(COALESCE(
                updates.payload #>> '{message,date}',
                updates.payload #>> '{callback_query,message,date}'
            )::double precision) ELSE NULL END,
            updates.received_at,
            CASE WHEN updates.payload ? 'callback_query' THEN 'action' ELSE 'text' END,
            COALESCE(
                updates.payload #>> '{message,text}',
                updates.payload #>> '{message,caption}'
            ),
            jsonb_build_object(
                'legacy_telegram_update_id', updates.id,
                'payload_retained_in', 'telegram_updates'
            ),
            1,
            updates.status,
            md5('legacy:telegram:operation:' || updates.id::text)::uuid,
            updates.id
        FROM telegram_updates AS updates CROSS JOIN people
        JOIN legacy_telegram_conversation_ids AS legacy ON legacy.owner_id = people.id
        """
    )
    op.execute(
        """
        INSERT INTO outbox_messages (
            id, owner_id, conversation_id, inbound_message_id, operation_id,
            intent, dedup_key, state, attempts, provider_reference, legacy_key,
            created_at, updated_at
        )
        SELECT
            md5('legacy:outbox:' || state.key)::uuid,
            people.id,
            legacy.conversation_id,
            CASE WHEN split_part(state.key, ':', 3) ~ '^[0-9]+$'
                      AND EXISTS (
                          SELECT 1 FROM telegram_updates
                          WHERE id = split_part(state.key, ':', 3)::bigint
                      )
                 THEN md5('legacy:telegram:update:' || split_part(state.key, ':', 3))::uuid
                 ELSE NULL END,
            CASE WHEN split_part(state.key, ':', 3) ~ '^[0-9]+$'
                 THEN md5('legacy:telegram:operation:' || split_part(state.key, ':', 3))::uuid
                 ELSE md5('legacy:outbox:operation:' || state.key)::uuid END,
            jsonb_build_object(
                'legacy_key', state.key,
                'text', state.value ->> 'text',
                'keyboard', state.value -> 'keyboard'
            ),
            'legacy:' || state.key,
            CASE state.value ->> 'status'
                WHEN 'pending' THEN 'queued'
                WHEN 'sending' THEN 'uncertain'
                WHEN 'sent' THEN 'provider_accepted'
                WHEN 'uncertain' THEN 'uncertain'
                WHEN 'failed' THEN 'failed'
                WHEN 'cancelled' THEN 'cancelled'
                ELSE 'uncertain'
            END,
            COALESCE((state.value ->> 'attempts')::integer, 0),
            state.value ->> 'message_id',
            state.key,
            state.updated_at,
            state.updated_at
        FROM app_state AS state CROSS JOIN people
        JOIN legacy_telegram_conversation_ids AS legacy ON legacy.owner_id = people.id
        WHERE state.key LIKE 'outbox:update:%'
        """
    )
    bind.execute(sa.text("DROP TABLE legacy_telegram_conversation_ids"))
    op.execute(
        """
        INSERT INTO message_delivery_receipts (
            id, outbox_message_id, state, observed_at, provider_reference
        )
        SELECT
            md5('legacy:receipt:' || legacy_key)::uuid,
            id,
            'provider_accepted',
            updated_at,
            provider_reference
        FROM outbox_messages
        WHERE state = 'provider_accepted'
        """
    )


def downgrade():
    op.drop_index(
        "ix_message_delivery_receipts_outbox_message_id",
        table_name="message_delivery_receipts",
    )
    op.drop_table("message_delivery_receipts")
    op.drop_index("ix_outbox_messages_state", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_operation_id", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_inbound_message_id", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_conversation_id", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_owner_id", table_name="outbox_messages")
    op.drop_table("outbox_messages")
    op.drop_index("ix_inbound_messages_status", table_name="inbound_messages")
    op.drop_index("ix_inbound_retention_age", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_operation_id", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_conversation_id", table_name="inbound_messages")
    op.drop_index("ix_inbound_messages_owner_id", table_name="inbound_messages")
    op.drop_table("inbound_messages")
    op.drop_index("ix_conversations_owner_id", table_name="conversations")
    op.drop_table("conversations")
