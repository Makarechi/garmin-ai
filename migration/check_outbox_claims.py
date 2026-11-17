"""Capture reply claims and memory fences from the pinned Python inbox/outbox implementation."""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.channels import OutboundIntent
from garmin_ai.dialogue import claim_outbox
from garmin_ai.models import Conversation, InboundMessage, OutboxMessage, Person

NOW = datetime.fromisoformat("2026-11-05T10:00:00+00:00")
CONVERSATION = 180
SOURCE = 181
OPERATION = 182
TARGET_EPOCH = 183
SOURCE_EPOCH = 184


def uid(value):
    return str(UUID(int=value))


def seed(session):
    owner = session.scalar(select(Person.id))
    for number, instance, epoch in [
        (CONVERSATION, "primary", TARGET_EPOCH),
        (SOURCE, "other", SOURCE_EPOCH),
    ]:
        session.add(
            Conversation(
                id=UUID(int=number),
                owner_id=owner,
                channel="fixture",
                channel_instance_id=instance,
                memory_epoch=UUID(int=epoch),
                share_owner_memory=True,
            )
        )
    session.flush()
    for number, revision in [(201, 1), (202, 2)]:
        session.add(
            InboundMessage(
                id=UUID(int=number),
                owner_id=owner,
                conversation_id=UUID(int=CONVERSATION),
                channel="fixture",
                channel_instance_id="primary",
                external_event_id=f"synthetic-{revision}",
                sender_ref="synthetic-owner",
                kind="text" if revision == 1 else "edit",
                received_at=NOW,
                envelope={},
                revision=revision,
                status="processed",
                operation_id=UUID(int=OPERATION),
            )
        )
    session.flush()
    result = []
    for number, inbound, target, source in [
        (1001, 202, TARGET_EPOCH - 1, SOURCE_EPOCH),
        (1002, 202, TARGET_EPOCH, SOURCE_EPOCH - 1),
        (1003, 201, TARGET_EPOCH, SOURCE_EPOCH),
        (1004, 202, TARGET_EPOCH, SOURCE_EPOCH),
        (1005, None, None, None),
        (1006, None, None, None),
    ]:
        intent = OutboundIntent.model_validate(
            {
                "intent_id": uid(number),
                "owner_id": str(owner),
                "conversation_id": uid(CONVERSATION),
                "channel_instance": {"channel": "fixture", "instance_id": "primary"},
                "blocks": [{"text": "synthetic reply"}],
            }
        )
        next_attempt = NOW + timedelta(minutes=1) if number == 1006 else None
        fence = (
            {"target_epoch": uid(target), "source_epochs": {uid(SOURCE): uid(source)}}
            if target
            else None
        )
        data = {
            "id": intent.intent_id,
            "conversation_id": intent.conversation_id,
            "dedup_key": f"synthetic-outbox-claim-{number}",
            "intent": intent.model_dump(mode="json"),
            "memory_fence": fence,
            "inbound_message_id": UUID(int=inbound) if inbound else None,
            "operation_id": UUID(int=OPERATION),
            "next_attempt_at": next_attempt,
        }
        session.add(OutboxMessage(owner_id=owner, **data))
        result.append(
            {
                key: str(value)
                if isinstance(value, UUID)
                else value.isoformat()
                if isinstance(value, datetime)
                else value
                for key, value in data.items()
            }
        )
    session.flush()
    return result


def snapshot(session):
    return [
        [
            str(row.id),
            row.state,
            row.attempts,
            row.lease_token is not None,
            row.lease_until.isoformat() if row.lease_until else None,
        ]
        for row in session.scalars(select(OutboxMessage).order_by(OutboxMessage.id))
    ]


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            initial = seed(session)
        checks = []
        for name, instant, seconds in [
            ("first", NOW, 120),
            ("second", NOW, 120),
            ("no_due", NOW, 120),
            ("invalid_duration", NOW + timedelta(minutes=1), 0),
            ("becomes_due", NOW + timedelta(minutes=1), 1),
            ("no_duplicate", NOW + timedelta(minutes=1), 120),
        ]:
            with Session(engine) as session, session.begin():
                try:
                    receipt = claim_outbox(session, instant, lease_for=timedelta(seconds=seconds))
                    result = str(receipt.outbox_message_id) if receipt else None
                    if name == "first":
                        assert receipt is not None and receipt.intent is None
                except ValueError:
                    result = "InvalidRequest"
                checks.append(
                    {
                        "name": name,
                        "now": instant.isoformat(),
                        "seconds": seconds,
                        "result": result,
                        "rows": snapshot(session),
                    }
                )
        output.write_text(json.dumps({"outbox": initial, "checks": checks}), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
