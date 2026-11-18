"""Compare historical Telegram conversation ID repair with pinned Alembic."""

import argparse
import os
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID, uuid5

from check_native_schema import CURRENT, compare_catalog, engine_for, run_native, upgrade
from sqlalchemy import text
from sqlalchemy.engine import make_url

BEFORE_REPAIR = "a42d9e18c701"
NAMESPACE = UUID("5ddd62fc-6890-44b6-86a2-20f77524378f")
OWNER = UUID(int=1)


def target(chat):
    return uuid5(NAMESPACE, f"{OWNER}:telegram:telegram:primary:{chat}")


def add_conversation(connection, identity, *, channel, instance, chat, state, day):
    connection.execute(
        text("""
        INSERT INTO conversations(id,owner_id,channel,channel_instance_id,external_conversation_id,
           memory_epoch,state,share_owner_memory,created_at,updated_at)
        VALUES (:id,:owner,:channel,:instance,:chat,:epoch,jsonb_build_object('state',CAST(:state AS text)),
                true,CAST(:day AS timestamptz),CAST(:day AS timestamptz))
        """),
        {
            "id": identity,
            "owner": OWNER,
            "channel": channel,
            "instance": instance,
            "chat": chat,
            "epoch": UUID(int=2500),
            "state": state,
            "day": day,
        },
    )


def seed(engine):
    upgrade(engine, BEFORE_REPAIR)
    with engine.begin() as connection:
        connection.execute(text("UPDATE people SET id=:owner WHERE singleton"), {"owner": OWNER})
        for number in range(130):
            chat = str(910000 + number)
            add_conversation(
                connection,
                UUID(int=1000 + number),
                channel="telegram",
                instance="telegram:primary",
                chat=chat,
                state=f"synthetic-{number}",
                day="2026-10-01T00:00:00Z",
            )
        for identity, channel, instance, chat, state in [
            (target("910001"), "telegram", "telegram:primary", None, "already present"),
            (target("910600"), "telegram", "telegram:primary", "910600", "already canonical"),
            (UUID(int=2200), "telegram", "telegram:primary", None, "no external chat"),
            (UUID(int=2201), "web", "web:primary", "910601", "other channel"),
        ]:
            add_conversation(
                connection,
                identity,
                channel=channel,
                instance=instance,
                chat=chat,
                state=state,
                day="2026-10-02T00:00:00Z",
            )
        for index in (0, 1):
            values = {
                "index": str(index),
                "inbound": UUID(int=2000 + index),
                "outbox": UUID(int=2100 + index),
                "operation": UUID(int=2300 + index),
                "conversation": UUID(int=1000 + index),
                "owner": OWNER,
            }
            connection.execute(
                text("""
                INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,
                    external_event_id,sender_ref,received_at,kind,normalized_text,envelope,operation_id)
                VALUES (:inbound,:owner,:conversation,'telegram','telegram:primary',:index,
                        'synthetic sender','2026-10-03T00:00:00Z','text','synthetic question','{}',:operation)
                """),
                values,
            )
            connection.execute(
                text("""
                INSERT INTO outbox_messages(id,owner_id,conversation_id,inbound_message_id,operation_id,
                    intent,dedup_key,state,created_at,updated_at)
                VALUES (:outbox,:owner,:conversation,:inbound,:operation,'{}',:index,'queued',
                        '2026-10-03T00:00:00Z','2026-10-03T00:00:00Z')
                """),
                values,
            )


def transported_rows(engine):
    with engine.connect() as connection:
        return tuple(
            connection.execute(text(statement)).all()
            for statement in (
                "SELECT * FROM conversations ORDER BY id",
                "SELECT * FROM inbound_messages ORDER BY id",
                "SELECT * FROM outbox_messages ORDER BY id",
            )
        )


def compare(binary, reference, native):
    for engine in (reference, native):
        seed(engine)
    conflicting = target("910002")
    with native.begin() as connection:
        add_conversation(
            connection,
            conflicting,
            channel="web",
            instance="web:conflict",
            chat=None,
            state="conflicting owner channel",
            day="2026-10-04T00:00:00Z",
        )
    before = transported_rows(native)
    run_native(binary, native, "telegram_collision")
    assert transported_rows(native) == before
    with native.begin() as connection:
        connection.execute(text("DELETE FROM conversations WHERE id=:id"), {"id": conflicting})
    upgrade(reference, CURRENT)
    run_native(binary, native, "before_telegram_repair")
    compare_catalog(reference, native)
    assert transported_rows(reference) == transported_rows(native)
    conversations, inbound, outbox = transported_rows(native)
    assert len(conversations) == 133
    assert {row.conversation_id for row in inbound} == {target("910000"), target("910001")}
    assert {row.conversation_id for row in outbox} == {target("910000"), target("910001")}
    assert next(row.state for row in conversations if row.id == target("910001")) == {
        "state": "already present"
    }
    print(
        "Matched 133 Telegram and web conversations, inbound history and outbox after rollback/retry"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("name_prefix")
    args = parser.parse_args()
    opening = make_url(os.environ["GA_TEST_DATABASE_URL"])
    assert opening.database and opening.database.endswith("_test")
    with ExitStack() as stack:
        reference, native = [
            engine_for(stack, opening.set(database=f"{args.name_prefix}_{kind}_test"))
            for kind in ("py", "rs")
        ]
        compare(args.binary.resolve(strict=True), reference, native)


if __name__ == "__main__":
    main()
