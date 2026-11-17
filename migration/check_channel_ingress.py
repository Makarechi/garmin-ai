"""Record synthetic neutral inbox validation and storage from pinned Python."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.channels import InboundEnvelope
from garmin_ai.dialogue import ingest_envelope
from garmin_ai.events import Conflict
from garmin_ai.models import InboundMessage, Person

CONVERSATION = str(UUID(int=20))
SECONDARY = str(UUID(int=21))
ACTION = str(UUID(int=22))


def cases(owner):
    base = {
        "message_id": str(UUID(int=1)),
        "owner_id": str(owner),
        "conversation_id": CONVERSATION,
        "channel_instance": {"channel": "fixture", "instance_id": "primary"},
        "external_event_id": "opaque-1",
        "external_message_id": "001/opaque",
        "sender_ref": "synthetic-owner",
        "kind": "text",
        "text": "synthetic ☕",
        "received_at": "2026-11-10T23:00:05.001+05:30",
        "occurred_at": "2026-11-10T17:30:01Z",
    }
    action = {
        "action_id": "confirm",
        "label": "synthetic acknowledgement",
        "operation_id": ACTION,
        "token": "synthetic-token-value",
        "expires_at": "2026-11-10T18:00:00Z",
    }
    variants = [
        ("new_text", {}),
        (
            "duplicate_ignores_new_body",
            {"message_id": str(UUID(int=2)), "text": "synthetic duplicate"},
        ),
        (
            "linked_edit",
            {
                "message_id": str(UUID(int=3)),
                "external_event_id": "opaque-2",
                "revision": "2",
                "kind": "edit",
                "text": "synthetic edit",
            },
        ),
        (
            "duplicate_edit",
            {
                "message_id": str(UUID(int=4)),
                "external_event_id": "opaque-2",
                "revision": 2,
                "kind": "edit",
            },
        ),
        (
            "old_edit",
            {
                "message_id": str(UUID(int=5)),
                "external_event_id": "opaque-old",
                "revision": 1,
                "kind": "edit",
            },
        ),
        (
            "missing_prior",
            {
                "message_id": str(UUID(int=6)),
                "external_event_id": "opaque-missing",
                "external_message_id": "other",
                "revision": 2,
                "kind": "edit",
            },
        ),
        (
            "action",
            {
                "message_id": str(UUID(int=7)),
                "external_event_id": "opaque-action",
                "kind": "action",
                "text": None,
                "action": action,
            },
        ),
        (
            "other_instance",
            {
                "message_id": str(UUID(int=8)),
                "conversation_id": SECONDARY,
                "channel_instance": {"channel": "fixture", "instance_id": "secondary"},
            },
        ),
        (
            "foreign_current_conversation",
            {
                "message_id": str(UUID(int=9)),
                "channel_instance": {"channel": "fixture", "instance_id": "secondary"},
            },
        ),
        (
            "audio",
            {
                "message_id": str(UUID(int=10)),
                "external_event_id": "opaque-audio",
                "kind": "voice",
                "attachments": [
                    {
                        "attachment_id": str(UUID(int=100)),
                        "kind": "voice",
                        "media_type": "audio/ogg",
                        "filename": "synthetic.ogg",
                        "size_bytes": 1234,
                        "external_id": "opaque-audio-id",
                    }
                ],
                "reply_to": {
                    "channel_instance": {"channel": "fixture", "instance_id": "primary"},
                    "external_message_id": "001/opaque",
                },
                "time_precision": "second",
            },
        ),
        (
            "receipt",
            {
                "message_id": str(UUID(int=11)),
                "external_event_id": "opaque-receipt",
                "kind": "receipt",
                "text": None,
                "occurred_at": None,
            },
        ),
        ("unknown_envelope_field", {"unknown": "synthetic unknown"}),
        ("unknown_attachment_field", {"attachments": [{"kind": "audio", "unknown": False}]}),
        ("missing_action", {"kind": "action"}),
        ("short_action_token", {"kind": "action", "action": {**action, "token": "short"}}),
        ("missing_voice_text", {"kind": "voice", "text": None}),
        ("invalid_namespace", {"channel_instance": {"channel": "fixture", "instance_id": ""}}),
        ("missing_timezone", {"received_at": "2026-11-10T23:00:00"}),
        ("overflow_text_limit", {"text": "🧪" * 100_001}),
        (
            "maximum_unicode_text",
            {
                "message_id": str(UUID(int=19)),
                "external_event_id": "opaque-bulk",
                "text": "🧪" * 100_000,
            },
        ),
        ("opaque_numeric_identifier_rejected", {"external_event_id": 42}),
        ("nonpositive_revision", {"revision": 0}),
    ]
    return [(name, {**base, **override}) for name, override in variants]


def record(path):
    engine = isolated_engine()
    try:
        with Session(engine) as session:
            owner = session.scalar(select(Person.id))
        records = []
        operations = {UUID(ACTION): ACTION}
        for name, body in cases(owner):
            try:
                envelope = InboundEnvelope.model_validate(body)
                with Session(engine) as session, session.begin():
                    stored, created = ingest_envelope(session, envelope)
                    operation = operations.setdefault(stored.operation_id, str(stored.id))
                    existing = {
                        "id": str(stored.id),
                        "created": created,
                        "operation": operation,
                        "supersedes": str(stored.supersedes_id) if stored.supersedes_id else None,
                        "envelope": stored.envelope,
                        "status": stored.status,
                    }
                expected = {"stored": existing, "normalized": envelope.model_dump(mode="json")}
            except ValidationError:
                expected = {"error": "invalid"}
            except PermissionError:
                expected = {"error": "ForeignConversation"}
            except Conflict as exc:
                expected = {
                    "error": "OldRevision" if "not newer" in str(exc) else "MissingRevision"
                }
            records.append({"name": name, "input": body, **expected})
        with Session(engine) as session:
            count = len(session.scalars(select(InboundMessage.id)).all())
        path.write_text(
            json.dumps({"cases": records, "messages": count}, ensure_ascii=False), encoding="utf-8"
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    record(parser.parse_args().output)
