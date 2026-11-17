"""Record the pinned Python channel-neutral intent contract using synthetic content."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from garmin_ai.channels import OutboundIntent


def capture(output):
    namespace = {"channel": "fixture", "instance_id": "primary"}
    block = {"text": "Synthetic reply ☕"}
    original = {
        "intent_id": str(UUID(int=401)),
        "owner_id": str(UUID(int=402)),
        "conversation_id": str(UUID(int=403)),
        "channel_instance": namespace,
        "blocks": [block],
    }
    action = {
        "action_id": "confirm",
        "label": "Synthetic confirmation",
        "operation_id": str(UUID(int=404)),
        "expires_at": "2026-11-15T11:01:02.1+05:00",
    }
    attachment = {
        "attachment_id": str(UUID(int=405)),
        "kind": "voice",
        "filename": "synthetic.ogg",
        "external_id": "opaque/file",
        "media_type": "audio/ogg",
        "size_bytes": "828",
    }
    reference = {"channel_instance": namespace, "external_message_id": "opaque/0001"}
    form = {"definition_key": "user.synthetic", "definition_version": "3.000"}
    cases = [
        ("defaults", {}),
        (
            "full",
            {
                "actions": [action],
                "attachments": [attachment],
                "form": form,
                "evidence_refs": ["definition:synthetic", "source:synthetic"],
                "initiative": "yes",
                "privacy": "shared",
                "priority": "-2",
                "preferred_medium": "voice",
                "policy_revision": "a" * 64,
                "logical_notification_id": "synthetic",
                "scheduled_day": "2026-11-15",
                "reply_to": reference,
                "replaces": reference,
                "expires_at": "2026-11-15T12:00:03.002-04:30",
            },
        ),
        ("form_only", {"blocks": [], "form": form}),
        ("action_only", {"blocks": [], "actions": [action]}),
        ("attachment_only", {"blocks": [], "attachments": [attachment]}),
        ("empty", {"blocks": []}),
        ("opaque_reply", {"reply_to": reference}),
        (
            "foreign_reply",
            {
                "reply_to": {
                    **reference,
                    "channel_instance": {"channel": "fixture", "instance_id": "other"},
                }
            },
        ),
        (
            "foreign_replaces",
            {
                "replaces": {
                    **reference,
                    "channel_instance": {"channel": "other", "instance_id": "primary"},
                }
            },
        ),
        ("priority_boundary", {"priority": 10}),
        ("priority_fraction", {"priority": 3.1}),
        ("priority_outside", {"priority": -11}),
        ("invalid_medium", {"preferred_medium": "audio"}),
        ("optional_dates", {"expires_at": None, "scheduled_day": None}),
        ("midnight_datetime", {"scheduled_day": "2026-11-15T00:00:00+05:30"}),
        ("timestamp_day", {"scheduled_day": int(datetime(2026, 11, 15, tzinfo=UTC).timestamp())}),
        ("timestamp_with_local_time", {"scheduled_day": "2026-11-15T00:00:01+05:30"}),
        ("dated_without_timezone", {"expires_at": "2026-11-15T12:00:00"}),
        ("non_string_evidence", {"evidence_refs": [40]}),
        ("unknown_root_key", {"unknown": True}),
        ("unknown_nested_key", {"blocks": [{**block, "html": False}]}),
        ("empty_block", {"blocks": [{"text": ""}]}),
        ("maximum_blocks", {"blocks": [block] * 100}),
        ("too_many_blocks", {"blocks": [block] * 101}),
        ("maximum_unicode", {"blocks": [{"text": "🧪" * 100_000}]}),
        ("too_much_unicode", {"blocks": [{"text": "🧪" * 100_001}]}),
        ("invalid_policy_revision", {"policy_revision": "A" * 64}),
        ("invalid_action_token", {"actions": [{**action, "token": "short"}]}),
        ("null_array", {"blocks": None}),
    ]
    output_rows = []
    for name, changes in cases:
        request = {**original, **changes}
        row = {"name": name, "input": request}
        try:
            row["expected"] = OutboundIntent.model_validate(request).model_dump(mode="json")
        except ValidationError:
            row["expected"] = None
        output_rows.append(row)
    output.write_text(json.dumps(output_rows, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
