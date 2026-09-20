"""Add the instance owner and explicit external connection bindings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e6b8f0a13c72"
down_revision = "d31e572abc90"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "people",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("singleton", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("locale", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("units", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("singleton", name="ck_people_single_owner"),
        sa.CheckConstraint("units IN ('metric', 'imperial')", name="ck_people_units"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton", name="uq_people_singleton"),
    )
    op.create_table(
        "channel_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("channel_instance_id", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("confirmation_method", sa.String(), nullable=False),
        sa.Column(
            "confirmed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel",
            "channel_instance_id",
            "external_id",
            name="uq_channel_external_identity",
        ),
        sa.UniqueConstraint(
            "owner_id", "channel", "channel_instance_id", name="uq_owner_channel_instance"
        ),
    )
    op.create_index("ix_channel_bindings_owner_id", "channel_bindings", ["owner_id"])
    op.create_table(
        "source_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("confirmation_method", sa.String(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "confirmed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "provider", "namespace", name="uq_owner_source_namespace"),
        sa.UniqueConstraint(
            "provider", "namespace", "external_id", name="uq_source_external_identity"
        ),
    )
    op.create_index("ix_source_connections_owner_id", "source_connections", ["owner_id"])
    op.execute(
        """
        INSERT INTO people (id, singleton, locale, timezone, units)
        VALUES (gen_random_uuid(), TRUE, 'ru', 'Europe/Bratislava', 'metric')
        """
    )
    op.execute(
        """
        INSERT INTO source_connections (
            id, owner_id, provider, namespace, external_id, confirmation_method, details
        )
        SELECT
            gen_random_uuid(),
            (SELECT id FROM people),
            'garmin',
            'socialProfile.profileId:v1',
            value ->> 'fingerprint',
            'legacy_account_binding',
            jsonb_build_object(
                'instance_id', value ->> 'instance_id',
                'identity_contract', value ->> 'identity_contract'
            )
        FROM app_state
        WHERE key = 'account:garmin'
          AND value ->> 'fingerprint' IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE app_state
        SET value = jsonb_set(
            value,
            '{owner_id}',
            to_jsonb((SELECT id::text FROM people)),
            TRUE
        )
        WHERE key = 'preferences:personal-goals'
        """
    )


def downgrade():
    op.execute(
        "UPDATE app_state SET value = value - 'owner_id' WHERE key = 'preferences:personal-goals'"
    )
    op.drop_index("ix_source_connections_owner_id", table_name="source_connections")
    op.drop_table("source_connections")
    op.drop_index("ix_channel_bindings_owner_id", table_name="channel_bindings")
    op.drop_table("channel_bindings")
    op.drop_table("people")
