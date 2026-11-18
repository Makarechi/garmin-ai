"""Compare the initial neutral Telegram backfill repair on disposable PostgreSQL."""

import argparse
import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID, uuid5

from check_native_schema import CURRENT, compare_catalog, engine_for, run_native, upgrade
from check_telegram_schema import NAMESPACE, OWNER, add_conversation, transported_rows
from sqlalchemy import text
from sqlalchemy.engine import make_url

BEFORE_REPAIR = "f103aa712b44"
SOURCE_ID = UUID(int=1123)
LEGACY_ID = UUID(
    hashlib.md5(f"legacy:telegram:conversation:{OWNER}".encode(), usedforsecurity=False).hexdigest()
)


def seed(engine, case):
    upgrade(engine, BEFORE_REPAIR)
    voice = {"file_id": "synthetic-voice", "file_size": 127}
    if case == "binding":
        voice["mime_type"] = "audio/mpeg"
    message = {"chat": {"id": 9002}, "caption": "synthetic caption", "voice": voice}
    if case == "binding":
        message["text"] = "synthetic text"
    with engine.begin() as connection:
        connection.execute(text("UPDATE people SET id=:owner WHERE singleton"), {"owner": OWNER})
        add_conversation(
            connection,
            LEGACY_ID,
            channel="telegram",
            instance="primary",
            chat=None,
            state="legacy synthetic history",
            day="2026-10-01T00:00:00Z",
        )
        if case == "binding":
            connection.execute(
                text("""
                INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
                VALUES(gen_random_uuid(),:owner,'telegram','primary','9001','synthetic')
                """),
                {"owner": OWNER},
            )
        if case == "callback":
            connection.execute(
                text("""
                INSERT INTO telegram_updates(id,payload,received_at,status)
                VALUES(54,CAST(:payload AS jsonb),
                       '2026-10-02T00:00:00Z','processed')
                """),
                {"payload": json.dumps({"callback_query": {"message": {"chat": {"id": 9003}}}})},
            )
        raw = None if case == "unbound" else 55
        if raw is not None:
            connection.execute(
                text("""
                INSERT INTO telegram_updates(id,payload,received_at,status)
                VALUES(55,CAST(:payload AS jsonb),'2026-10-03T00:00:00Z','processed')
                """),
                {"payload": json.dumps({"message": message})},
            )
        params = {
            "inbound": SOURCE_ID,
            "owner": OWNER,
            "conversation": LEGACY_ID,
            "operation": UUID(int=3000),
            "outbox": UUID(int=3001),
            "raw": raw,
        }
        connection.execute(
            text("""
            INSERT INTO inbound_messages(id,owner_id,conversation_id,channel,channel_instance_id,
                external_event_id,sender_ref,received_at,kind,normalized_text,envelope,
                operation_id,legacy_telegram_update_id)
            VALUES(:inbound,:owner,:conversation,'telegram','primary','synthetic-event','synthetic-sender',
                '2026-10-03T00:00:00Z','text','unrepaired message',jsonb_build_object('kept',true),:operation,:raw)
            """),
            params,
        )
        connection.execute(
            text("""
            INSERT INTO outbox_messages(id,owner_id,conversation_id,inbound_message_id,operation_id,
                intent,dedup_key,state,created_at,updated_at)
            VALUES(:outbox,:owner,:conversation,:inbound,:operation,'{}','synthetic-reply','queued',
                '2026-10-03T00:00:00Z','2026-10-03T00:00:00Z')
            """),
            params,
        )


def compare(binary, reference, native, case, chat):
    for engine in (reference, native):
        seed(engine, case)
    upgrade(reference, CURRENT)
    run_native(binary, native, "before_telegram_backfill")
    compare_catalog(reference, native)
    assert transported_rows(reference) == transported_rows(native)
    conversations, inbound, outbox = transported_rows(native)
    canonical = uuid5(NAMESPACE, f"{OWNER}:telegram:primary:{chat}")
    assert len(conversations) == len(inbound) == len(outbox) == 1
    assert (
        conversations[0].id == inbound[0].conversation_id == outbox[0].conversation_id == canonical
    )
    assert conversations[0].external_conversation_id == chat
    assert inbound[0].envelope["kept"] is True
    if case == "unbound":
        assert inbound[0].kind == "text" and inbound[0].normalized_text == "unrepaired message"
    else:
        assert inbound[0].kind == "voice"
        assert inbound[0].normalized_text == (
            "synthetic text" if case == "binding" else "synthetic caption"
        )
        assert inbound[0].envelope["attachments"] == [
            {
                "kind": "voice",
                "external_id": "synthetic-voice",
                "size_bytes": 127,
                "media_type": "audio/mpeg" if case == "binding" else "audio/ogg",
            }
        ]
    print(f"Matched the historical Telegram repair supplied by {case}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("name_prefix")
    options = parser.parse_args()
    opening = make_url(os.environ["GA_TEST_DATABASE_URL"])
    assert opening.database and opening.database.endswith("_test")
    with ExitStack() as stack:
        for case, chat in (
            ("binding", "9001"),
            ("raw", "9002"),
            ("callback", "9003"),
            ("unbound", "legacy-owner"),
        ):
            left, right = [
                engine_for(stack, opening.set(database=f"{options.name_prefix}_{case}_{kind}_test"))
                for kind in ("py", "rs")
            ]
            compare(options.binary.resolve(strict=True), left, right, case, chat)


if __name__ == "__main__":
    main()
