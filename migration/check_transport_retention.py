"""Compare the native retention command with the pinned Python application."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from garmin_ai.config import Settings
from garmin_ai.db import exclusive_ingestion
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import (
    AppState,
    Audit,
    Conversation,
    Event,
    InboundMessage,
    Job,
    MessageDeliveryReceipt,
    OutboxMessage,
    PendingQuestion,
    Person,
    TelegramUpdate,
)
from garmin_ai.retention import REDACTED_REPLY, prune_telegram_text
from garmin_ai.storage_files import exclusive_files

NOW = datetime.now(UTC).replace(microsecond=0)
OLD = NOW - timedelta(days=100)
RECENT = NOW - timedelta(days=5)
COMPACT_OLD = OLD.strftime("%Y%m%dT%H%M%SZ")
WEEK_OLD = OLD.strftime("%G-W%V-%uT%H%M%SZ")


def seed(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id))
        conversation = UUID(int=800)
        session.add(
            Conversation(
                id=conversation,
                owner_id=owner,
                channel="telegram",
                channel_instance_id="primary",
                external_conversation_id="synthetic-retention",
            )
        )
        session.flush()
        for identity, status, completion in (
            (-77, "done", OLD),
            (7, "pending", OLD),
            (8, "done", RECENT),
            (9, "done", OLD),
        ):
            session.add(
                TelegramUpdate(
                    id=identity,
                    payload={
                        "text": "synthetic legacy transport",
                        "update_id": abs(identity),
                        "_ordering_epoch": 7,
                        "_channel_instance": {"channel": "telegram", "instance_id": "secondary"},
                    },
                    received_at=OLD,
                    status="processed",
                )
            )
            session.add(
                Job(
                    id=UUID(int=abs(identity)),
                    kind="telegram_update",
                    dedup_key=f"telegram:{identity}",
                    payload={"update_id": identity, "text": "synthetic legacy job"},
                    status=status,
                    completed_at=completion,
                    run_at=OLD,
                )
            )
            session.add(
                AppState(
                    key=f"telegram:reply:{identity}",
                    value={"text": "synthetic legacy reply", "kind": "status"},
                )
            )
        session.add_all(
            [
                AppState(key="telegram:transcript:-77", value={"text": "synthetic transcript"}),
                AppState(key="outbox:update:-77:0", value={"status": "sent", "message_id": 43}),
                AppState(
                    key="conversation:pending-old",
                    value={"created_at": COMPACT_OLD, "text": "synthetic old"},
                ),
                AppState(
                    key="conversation:pending-naive",
                    value={"created_at": OLD.replace(tzinfo=None).isoformat(), "text": "synthetic"},
                ),
                AppState(
                    key="conversation:pending-recent",
                    value={"created_at": RECENT.isoformat(), "text": "synthetic"},
                ),
                AppState(
                    key="conversation:pending-unknown",
                    value={"created_at": COMPACT_OLD.replace("Z", "z"), "text": "synthetic"},
                ),
            ]
        )
        for index, answered in enumerate(
            [RECENT.isoformat(), "not-a-timestamp", COMPACT_OLD, WEEK_OLD], 101
        ):
            session.add(
                PendingQuestion(
                    id=UUID(int=index),
                    kind="synthetic",
                    text="synthetic question",
                    evidence={
                        "answer_text": "synthetic answer",
                        "answered_at": answered,
                        "choice": 2,
                    },
                    priority=1.0,
                    earliest_send_at=OLD,
                    expires_at=RECENT,
                    status="answered",
                    dedup_key=f"answer:{index}",
                )
            )
        for index, state in [(201, "delivered"), (202, "uncertain"), (203, "queued")]:
            session.add(
                InboundMessage(
                    id=UUID(int=index),
                    owner_id=owner,
                    conversation_id=conversation,
                    channel="telegram",
                    channel_instance_id="primary",
                    external_event_id=str(index),
                    sender_ref="owner",
                    received_at=OLD,
                    kind="text",
                    normalized_text="synthetic inbound",
                    envelope={"text": "synthetic inbound"},
                    status="processed",
                )
            )
            session.flush()
            session.add(
                OutboxMessage(
                    id=UUID(int=index + 10),
                    owner_id=owner,
                    conversation_id=conversation,
                    inbound_message_id=UUID(int=index),
                    operation_id=UUID(int=index + 20),
                    intent={"text": "synthetic outbox"},
                    dedup_key=f"outbox:{index}",
                    state=state,
                    created_at=OLD,
                )
            )
        for index in [250, 251, 252]:
            session.add(
                OutboxMessage(
                    id=UUID(int=index),
                    owner_id=owner,
                    conversation_id=conversation,
                    operation_id=UUID(int=index + 20),
                    intent={"text": "synthetic orphan"},
                    dedup_key=f"outbox:{index}",
                    state="failed",
                    created_at=OLD,
                )
            )
        session.flush()
        session.add(
            MessageDeliveryReceipt(
                id=UUID(int=900),
                outbox_message_id=UUID(int=211),
                state="delivered",
                observed_at=OLD,
                provider_reference="synthetic-receipt",
            )
        )
        create_event(
            session,
            EventInput(
                start=RECENT,
                payload={"type": "note", "description": "synthetic diary event"},
                original_text="synthetic diary fact stays",
            ),
            actor="api",
        )


def tombstone(value):
    if not value.get("_text_redacted") or "receipt" not in value:
        return value
    receipt = UUID(value["receipt"])
    assert receipt.version == 4
    return {**value, "receipt": "verified"}


def snapshot(engine):
    with Session(engine) as session:
        inbound = list(session.scalars(select(InboundMessage).order_by(InboundMessage.id)))
        outbox = list(session.scalars(select(OutboxMessage).order_by(OutboxMessage.id)))
        by_id = {entry.id: entry.envelope for entry in inbound}
        for item in outbox:
            if item.inbound_message_id and item.intent.get("_text_redacted"):
                assert item.intent == by_id[item.inbound_message_id]
        return {
            "telegram": [
                (row.id, row.status, tombstone(row.payload))
                for row in session.scalars(select(TelegramUpdate).order_by(TelegramUpdate.id))
            ],
            "jobs": [
                (row.id, row.status, row.payload, row.dedup_key)
                for row in session.scalars(select(Job).order_by(Job.id))
            ],
            "state": dict(
                session.execute(
                    select(AppState.key, AppState.value).where(
                        AppState.key.startswith("telegram:")
                        | AppState.key.startswith("conversation:pending")
                        | AppState.key.startswith("outbox:")
                    )
                ).all()
            ),
            "inbound": [
                (row.id, row.status, row.normalized_text, tombstone(row.envelope))
                for row in inbound
            ],
            "outbox": [(row.id, row.state, row.dedup_key, tombstone(row.intent)) for row in outbox],
            "answers": list(
                session.execute(
                    select(PendingQuestion.id, PendingQuestion.evidence).order_by(
                        PendingQuestion.id
                    )
                ).all()
            ),
            "delivery": list(
                session.execute(
                    select(MessageDeliveryReceipt.id, MessageDeliveryReceipt.provider_reference)
                ).all()
            ),
            "facts": list(session.scalars(select(Event.original_text))),
            "audits": list(session.scalars(select(Audit.action))),
        }


def call(binary, root, config, *, succeeds=True, **options):
    args = [binary, "prune-telegram-text"]
    for key, value in options.items():
        if value is True:
            args.append("--" + key.replace("_", "-"))
        elif value is not None and value is not False:
            args.extend(("--" + key.replace("_", "-"), str(value)))
    result = subprocess.run(
        args,
        cwd=root,
        env={
            "GA_DATABASE_URL": config.database_url.get_secret_value(),
            "GA_LOCK_DIR": str(config.lock_dir),
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if not succeeds:
        assert result.returncode == 1 and not result.stdout and result.stderr
        return None
    assert result.returncode == 0 and not result.stderr, result.stderr
    return json.loads(result.stdout)


def run(binary, root, reference, native):
    seed(reference)
    seed(native)
    config = Settings(
        _env_file=None,
        database_url=native.url.render_as_string(hide_password=False),
        lock_dir=root / "locks",
    )
    previous = snapshot(reference)
    assert previous == snapshot(native)
    with exclusive_files(config):
        call(binary, root, config, apply=True, succeeds=False)
    with exclusive_ingestion(native):
        call(binary, root, config, apply=True, succeeds=False)
    call(binary, root, config, older_than_days=29, succeeds=False)
    call(binary, root, config, neutral_cursor="not-json", succeeds=False)
    assert snapshot(native) == previous

    def compare(**options):
        actual = call(binary, root, config, **options)
        clock = datetime.fromisoformat(actual["cutoff"]) + timedelta(days=90)
        with Session(reference) as session, session.begin():
            expected = prune_telegram_text(session, now=clock, **options)
        assert expected == actual, (expected, actual)
        assert snapshot(reference) == snapshot(native)
        return actual

    def wrapped_uuid(value):
        return f"urn:uuid:{{{UUID(int=value)}}}"

    offset = timezone(timedelta(hours=-2, microseconds=-500_000))
    offset_old = OLD.astimezone(offset).isoformat()
    for date, identity in [
        (COMPACT_OLD, " +７ "),
        (WEEK_OLD, 7.75),
        (offset_old, "0_7"),
        (OLD.isoformat(), True),
        (OLD.isoformat(), 2**63 - 1),
        (OLD.isoformat(), -(2**63)),
        ((OLD - timedelta(microseconds=1)).isoformat(), 2**63 - 1),
        ((OLD + timedelta(microseconds=1)).isoformat(), -(2**63)),
        ("00010101T000000+01", 0),
        ("99991231T235959-01", 0),
    ]:
        compare(cursor=json.dumps([date, identity]), limit=1)
    compare(cursor=json.dumps({COMPACT_OLD: None, " +７ ": None}), limit=1)
    compare(neutral_cursor=json.dumps([WEEK_OLD, wrapped_uuid(201)]), limit=1)
    for date in (offset_old, "00010101T000000+01", "99991231T235959-01"):
        result = compare(
            neutral_cursor=json.dumps(
                {
                    "inbound_after": [date, wrapped_uuid(203)],
                    "inbound_done": True,
                    "orphan_after": {COMPACT_OLD: None, wrapped_uuid(250): None},
                }
            ),
            limit=1,
        )
        assert json.loads(result["next_neutral_cursor"])["inbound_after"] == [
            datetime.fromisoformat(date).isoformat(),
            str(UUID(int=203)),
        ]

    for bad in (
        {"cursor": json.dumps([OLD.isoformat().replace("+00:00", "z"), 7])},
        {"cursor": json.dumps([OLD.isoformat(), 10**80])},
        {"cursor": json.dumps([OLD.isoformat(), -(10**80)])},
        {"neutral_cursor": json.dumps([COMPACT_OLD[:-1], wrapped_uuid(201)])},
        {"answer_cursor": f"URN:UUID:{UUID(int=103)}"},
    ):
        call(binary, root, config, apply=True, succeeds=False, **bad)
        try:
            with Session(reference) as session, session.begin():
                prune_telegram_text(session, now=NOW, apply=True, **bad)
        except (ValueError, DataError):
            pass
        else:
            raise AssertionError(f"Python accepted an invalid resume position: {bad}")

    cursor = {}
    for _ in range(5):
        page = compare(limit=2, **cursor)
        cursor = {key: page["next_" + key] for key in ("cursor", "answer_cursor", "neutral_cursor")}
    assert snapshot(native) == previous
    resumed = compare(
        cursor=json.dumps([WEEK_OLD, " +８ "]),
        answer_cursor=wrapped_uuid(103),
        neutral_cursor=json.dumps(
            {
                "inbound_after": [offset_old, wrapped_uuid(203)],
                "inbound_done": True,
                "orphan_after": [COMPACT_OLD, wrapped_uuid(250)],
            }
        ),
        limit=1,
        apply=True,
    )
    assert resumed["eligible_updates"] == 1
    assert resumed["eligible_neutral_messages"] == 1
    assert resumed["eligible_proactive_answers"] == 1
    resumed_snapshot = snapshot(native)
    assert [row[0] for row in resumed_snapshot["telegram"] if row[2].get("_text_redacted")] == [9]
    assert [row[0].int for row in resumed_snapshot["outbox"] if row[3].get("_text_redacted")] == [
        251
    ]
    assert [
        identity.int
        for identity, evidence in resumed_snapshot["answers"]
        if "answer_text" not in evidence
    ] == [104]
    redacted = compare(apply=True)
    assert redacted["eligible_updates"] == 1
    assert redacted["eligible_neutral_messages"] == 3
    assert redacted["eligible_proactive_answers"] == 1
    current = snapshot(native)
    assert current["state"]["telegram:reply:-77"]["text"] == REDACTED_REPLY
    assert current["state"]["outbox:update:-77:0"] == previous["state"]["outbox:update:-77:0"]
    assert current["delivery"] == previous["delivery"]
    assert current["facts"] == previous["facts"]
    assert current["audits"] == previous["audits"]
    assert "conversation:pending-old" not in current["state"]
    for suffix in ("naive", "recent", "unknown"):
        key = "conversation:pending-" + suffix
        assert current["state"][key] == previous["state"][key]
    assert compare(apply=True)["eligible_updates"] == 0
    assert snapshot(native) == current
    print(
        "Matched Python retention pages, apply, retry, durable receipts and unchanged diary facts"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    options = parser.parse_args()
    native_url = os.environ["GA_TEST_NATIVE_URL"]
    source = make_url(os.environ["GA_TEST_DATABASE_URL"])
    target = make_url(native_url)
    assert source.database != target.database and source.set(database=target.database) == target
    reference = isolated_engine()
    try:
        with patch.dict(os.environ, {"GA_TEST_DATABASE_URL": native_url}):
            native = isolated_engine()
        try:
            with TemporaryDirectory() as directory:
                run(
                    options.binary.resolve(strict=True),
                    Path(directory).resolve(),
                    reference,
                    native,
                )
        finally:
            native.dispose()
    finally:
        reference.dispose()


if __name__ == "__main__":
    main()
