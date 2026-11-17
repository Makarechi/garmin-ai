"""Record pinned Python outbox enqueue behavior with synthetic replies and actions."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from garmin_ai.channels import OutboundIntent
from garmin_ai.dialogue import queue_intent
from garmin_ai.models import AppState, Conversation, InboundMessage, OutboxMessage, Person

BASE = datetime.fromisoformat("2026-11-12T10:00:00+00:00")
MAIN = UUID(int=880)
SECONDARY = UUID(int=881)
INBOUND = UUID(int=882)
OPERATION = UUID(int=883)


class FixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return BASE.astimezone(tz) if tz else BASE.replace(tzinfo=None)


def seed(session):
    owner = session.scalar(select(Person.id))
    for conversation, instance in [(MAIN, "primary"), (SECONDARY, "secondary")]:
        session.add(
            Conversation(
                id=conversation, owner_id=owner, channel="fixture", channel_instance_id=instance
            )
        )
    session.flush()
    session.add(
        InboundMessage(
            id=INBOUND,
            owner_id=owner,
            conversation_id=MAIN,
            channel="fixture",
            channel_instance_id="primary",
            external_event_id="synthetic-origin",
            sender_ref="synthetic-owner",
            kind="edit",
            received_at=BASE,
            envelope={},
            revision=2,
            status="processed",
            operation_id=OPERATION,
        )
    )
    return owner


def cases(owner):
    ordinary = {
        "intent_id": str(UUID(int=891)),
        "owner_id": str(owner),
        "conversation_id": str(MAIN),
        "channel_instance": {"channel": "fixture", "instance_id": "primary"},
        "blocks": [{"text": "Synthetic queue reply"}],
    }
    action = {"action_id": "synthetic-confirm", "label": "Confirm", "operation_id": str(OPERATION)}
    explicit = {
        "action_id": "synthetic-skip",
        "label": "Skip",
        "operation_id": str(OPERATION),
        "expires_at": "2026-11-12T18:00:00.1+05:00",
    }
    cases = [
        ("text", 890, None, {}),
        (
            "duplicate_ignores_new_content",
            890,
            None,
            {"intent_id": str(UUID(int=892)), "blocks": [{"text": "Different"}]},
        ),
        (
            "signed_actions",
            OPERATION.int,
            INBOUND,
            {"intent_id": str(UUID(int=893)), "actions": [action, explicit]},
        ),
        (
            "retry_preserves_actions",
            OPERATION.int,
            INBOUND,
            {"intent_id": str(UUID(int=894)), "actions": [explicit]},
        ),
        (
            "expired_after_one_issue",
            895,
            None,
            {
                "intent_id": str(UUID(int=895)),
                "actions": [action, {**explicit, "expires_at": "2026-11-12T09:00:00Z"}],
            },
        ),
        (
            "foreign_inbound",
            OPERATION.int,
            INBOUND,
            {
                "intent_id": str(UUID(int=896)),
                "conversation_id": str(SECONDARY),
                "channel_instance": {"channel": "fixture", "instance_id": "secondary"},
                "actions": [action],
            },
        ),
        (
            "missing_tracker",
            897,
            None,
            {"intent_id": str(UUID(int=897)), "evidence_refs": [f"definition:{UUID(int=898)}"]},
        ),
    ]
    # A deliberate custom request key lets the foreign-inbound case reach context validation.
    for name, operation, inbound, overrides in cases:
        key = "synthetic-foreign-request" if name == "foreign_inbound" else None
        yield (
            name,
            str(UUID(int=operation)),
            str(inbound) if inbound else None,
            key,
            {**ordinary, **overrides},
        )


def snapshot(session):
    outbox = []
    for row in session.scalars(select(OutboxMessage).order_by(OutboxMessage.id)):
        intent = dict(row.intent)
        intent["actions"] = [{**action, "token": None} for action in intent["actions"]]
        outbox.append(
            [
                str(row.id),
                str(row.operation_id),
                str(row.inbound_message_id) if row.inbound_message_id else None,
                row.dedup_key,
                row.state,
                row.attempts,
                intent,
            ]
        )
    tokens = session.scalar(
        select(func.count()).select_from(AppState).where(AppState.key.startswith("action-token:"))
    )
    return {"outbox": outbox, "tokens": tokens}


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            owner = seed(session)
        records = []
        with (
            patch("garmin_ai.dialogue.datetime", FixtureClock),
            patch("garmin_ai.action_tokens.datetime", FixtureClock),
        ):
            for name, operation, inbound, key, source in cases(owner):
                try:
                    with Session(engine) as session, session.begin():
                        intent = OutboundIntent.model_validate(source)
                        stored = queue_intent(
                            session,
                            intent,
                            operation_id=UUID(operation),
                            inbound_message_id=UUID(inbound) if inbound else None,
                            dedup_key=key,
                        )
                        outcome = {"id": str(stored.id), "created": stored.id == intent.intent_id}
                except (ValueError, PermissionError) as error:
                    outcome = type(error).__name__
                with Session(engine) as session:
                    records.append(
                        {
                            "name": name,
                            "operation": operation,
                            "inbound": inbound,
                            "key": key,
                            "intent": source,
                            "outcome": outcome,
                            "snapshot": snapshot(session),
                        }
                    )
        output.write_text(
            json.dumps({"clock": BASE.isoformat(), "cases": records}), encoding="utf-8"
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
