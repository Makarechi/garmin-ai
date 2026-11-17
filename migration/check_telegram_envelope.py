"""Record normalized Telegram updates from pinned Python using synthetic payloads."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

from garmin_ai.channels import ActionRef, ChannelInstanceRef
from garmin_ai.telegram_adapter import normalize_update

OWNER = 1234567890123
PERSON = UUID(int=580)
RECEIVED = datetime.fromisoformat("2026-11-05T10:15:00.001000+00:00")


def fixtures():
    message = {
        "message_id": 42,
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER},
        "date": 1793872800,
        "text": "synthetic напоминание 🧪",
    }
    callback = {
        "from": {"id": OWNER},
        "data": "synthetic callback data",
        "message": {**message, "from": {"id": 87}},
    }
    action = {
        "action_id": "confirm",
        "label": "synthetic label",
        "operation_id": str(UUID(int=581)),
        "expires_at": "2026-11-05T12:15:00+02:00",
    }
    variants = [
        ("text", {"message": message}, "primary", "unavailable", False, None),
        ("second_bot", {"message": message}, "secondary", "unavailable", False, None),
        (
            "reply",
            {"message": {**message, "reply_to_message": {"message_id": 0}}},
            "primary",
            "unavailable",
            False,
            None,
        ),
        (
            "caption",
            {"message": {**message, "text": "", "caption": "synthetic caption"}},
            "primary",
            "unavailable",
            False,
            None,
        ),
        (
            "empty_caption",
            {"message": {**message, "text": "", "caption": ""}},
            "primary",
            "unavailable",
            False,
            None,
        ),
        ("system", {"message": {**message, "text": None}}, "primary", "unavailable", False, None),
        (
            "voice",
            {
                "message": {
                    **message,
                    "text": None,
                    "voice": {"file_id": "opaque/voice", "file_size": 842},
                }
            },
            "primary",
            "unavailable",
            False,
            None,
        ),
        (
            "voice_caption",
            {
                "message": {
                    **message,
                    "text": None,
                    "caption": "synthetic voice",
                    "voice": {"file_id": "opaque/voice", "mime_type": "audio/mpeg"},
                }
            },
            "primary",
            "unavailable",
            False,
            None,
        ),
        (
            "edit",
            {"edited_message": {**message, "edit_date": 1793872860}},
            "primary",
            "unavailable",
            False,
            None,
        ),
        (
            "unknown_message_time",
            {"message": {**message, "date": True}},
            "primary",
            "unavailable",
            False,
            None,
        ),
        ("callback_known", {"callback_query": callback}, "primary", "verified", True, action),
        ("callback_unknown", {"callback_query": callback}, "secondary", "verified", False, action),
        (
            "legacy_callback",
            {"callback_query": {**callback, "data": None}},
            "primary",
            "legacy",
            False,
            None,
        ),
        (
            "foreign_sender",
            {"message": {**message, "from": {"id": OWNER + 1}}},
            "primary",
            "unavailable",
            False,
            None,
        ),
        (
            "foreign_chat",
            {"message": {**message, "chat": {"id": OWNER + 1, "type": "private"}}},
            "primary",
            "unavailable",
            False,
            None,
        ),
        (
            "group",
            {"message": {**message, "chat": {"id": OWNER, "type": "group"}}},
            "primary",
            "unavailable",
            False,
            None,
        ),
        (
            "unbound_callback",
            {"callback_query": {**callback, "message": None}},
            "primary",
            "unavailable",
            False,
            None,
        ),
    ]
    # An unsupported callback with no message is expressed as an absent message
    # in the transport, since the pinned adapter expects a mapping.
    for index, (name, fields, instance, resolution, known, action) in enumerate(variants, 510):
        update = {"update_id": index, **fields}
        if name == "unbound_callback":
            update["callback_query"].pop("message")
        if "callback_query" in update:
            update["_callback_time_known"] = known
        yield name, update, instance, resolution, known, action


def capture(output):
    result = []
    for name, update, instance, resolution, known, action in fixtures():
        expected = {
            "name": name,
            "update": update,
            "instance": instance,
            "resolution": resolution,
            "known": known,
            "action": action,
        }
        try:
            envelope = normalize_update(
                update,
                external_owner_id=OWNER,
                internal_owner_id=PERSON,
                received_at=RECEIVED,
                resolved_action=ActionRef.model_validate(action) if action else None,
                channel_instance=ChannelInstanceRef(channel="telegram", instance_id=instance),
            ).model_dump(mode="json")
            del envelope["message_id"]
            for part in envelope["attachments"]:
                del part["attachment_id"]
            expected["normalized"] = envelope
        except PermissionError:
            expected["error"] = "ForeignChat"
        result.append(expected)
    output.write_text(
        json.dumps(
            {
                "owner": OWNER,
                "person": str(PERSON),
                "received": RECEIVED.isoformat(),
                "cases": result,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
