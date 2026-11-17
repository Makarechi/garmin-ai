"""Seed Python-signed Telegram buttons on a disposable database for native callback tests."""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.channels import ActionRef, OutboundIntent
from garmin_ai.dialogue import ingest_envelope, queue_intent
from garmin_ai.models import Person
from garmin_ai.telegram_adapter import normalize_update

OWNER = 1234567890123
NOW = datetime.fromisoformat("2099-07-02T10:00:00+00:00")


class FixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            owner = session.scalar(select(Person.id))
            spoken = {
                "message_id": 101,
                "chat": {"id": OWNER, "type": "private"},
                "from": {"id": OWNER},
                "text": "synthetic original message",
            }
            first = normalize_update(
                {"update_id": 1001, "message": spoken},
                external_owner_id=OWNER,
                internal_owner_id=owner,
                received_at=NOW,
            )
            incoming, _ = ingest_envelope(session, first)
            results = {}
            with (
                patch("garmin_ai.dialogue.datetime", FixtureClock),
                patch("garmin_ai.action_tokens.datetime", FixtureClock),
            ):
                for name, number in [
                    ("valid", 9001),
                    ("rollback", 9002),
                    ("stale", 9003),
                    ("expired", 9004),
                ]:
                    causer = incoming if name == "stale" else None
                    operation = causer.operation_id if causer else UUID(int=number + 100)
                    action = ActionRef(
                        action_id=f"synthetic-{name}",
                        label="Confirm",
                        operation_id=UUID(int=number + 200),
                        expires_at=NOW + timedelta(seconds=1 if name == "expired" else 600),
                    )
                    intent = OutboundIntent.model_validate(
                        {
                            "intent_id": str(UUID(int=number)),
                            "owner_id": str(owner),
                            "conversation_id": str(first.conversation_id),
                            "channel_instance": first.channel_instance.model_dump(mode="json"),
                            "actions": [action.model_dump(mode="json")],
                        }
                    )
                    row = queue_intent(
                        session,
                        intent,
                        operation_id=operation,
                        inbound_message_id=causer.id if causer else None,
                        dedup_key=f"synthetic:{name}",
                    )
                    row.state = "provider_accepted"
                    body = {
                        "update_id": number,
                        "callback_query": {
                            "data": row.intent["actions"][0]["token"],
                            "from": {"id": OWNER},
                            "message": {**spoken, "from": {"id": 77}, "text": None},
                        },
                    }
                    results[name] = {
                        "update": body,
                        "outbox_id": str(row.id),
                        "operation": str(action.operation_id),
                        "action_id": action.action_id,
                    }
            edit = normalize_update(
                {"update_id": 1002, "edited_message": spoken},
                external_owner_id=OWNER,
                internal_owner_id=owner,
                received_at=NOW,
            )
            ingest_envelope(session, edit)
        output.write_text(
            json.dumps(
                {
                    "owner": str(owner),
                    "external_owner": OWNER,
                    "now": NOW.isoformat(),
                    "conversation": str(first.conversation_id),
                    "cases": results,
                }
            ),
            encoding="utf-8",
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
