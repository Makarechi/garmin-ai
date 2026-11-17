"""Capture neutral Telegram reply rendering from the pinned Python adapter."""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from garmin_ai.channels import OutboundIntent
from garmin_ai.telegram_adapter import TelegramChannel

CHAT = 1234567890123
OWNER = str(UUID(int=701))
NOW = datetime.fromisoformat("2026-11-17T19:00:00+00:00")
CHANNEL = {"channel": "telegram", "instance_id": "primary"}


class RecordingBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, *, chat_id, text, parse_mode, reply_markup):
        self.calls.append(
            {
                "chat": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "keyboard": reply_markup.to_dict()["inline_keyboard"] if reply_markup else [],
            }
        )
        return SimpleNamespace(message_id=len(self.calls))


async def capture(output):
    action = {
        "action_id": "synthetic-action",
        "operation_id": str(UUID(int=702)),
        "label": "Confirm 🧪",
        "token": "é" * 32,
        "expires_at": "2026-11-18T17:00:00+00:00",
    }
    reference = {"external_message_id": "105", "channel_instance": CHANNEL}
    docs = {
        "literal_markdown": {"blocks": [{"text": "**keep markup literal**"}]},
        "two_blocks": {"blocks": [{"text": "one"}, {"text": "two"}]},
        "codepoint_boundary": {"blocks": [{"text": "🧪" * 3499 + "н👩"}]},
        "button_first_part": {
            "blocks": [{"text": "a" * 3501}],
            "actions": [action, {**action, "label": "Second", "token": "b" * 57}],
        },
        "action_only": {"actions": [action]},
        "overlong_token": {"actions": [{**action, "token": "é" * 32 + "b"}]},
        "form_only": {"form": {"definition_key": "user.synthetic", "definition_version": 2}},
        "form_and_actions": {
            "form": {"definition_key": "user.synthetic", "definition_version": 4},
            "actions": [action],
        },
        "reply_and_replace": {
            "blocks": [{"text": "synthetic update"}],
            "replaces": reference,
            "reply_to": reference,
        },
        "unsupported_attachments": {
            "attachments": [{"kind": "file", "filename": "synthetic.csv", "size_bytes": 8}],
            "blocks": [{"text": "does not detach attachment"}],
        },
        "prefer_voice": {"blocks": [{"text": "send as text"}], "preferred_medium": "voice"},
        "expired_intent": {
            "blocks": [{"text": "expired"}],
            "expires_at": NOW.isoformat(),
        },
        "expired_action": {"actions": [{**action, "expires_at": NOW.isoformat()}]},
        "other_bot": {
            "channel_instance": {"channel": "telegram", "instance_id": "alternate"},
            "blocks": [{"text": "wrong bot"}],
        },
    }
    results = []
    for number, (name, fields) in enumerate(docs.items(), 710):
        intent = {
            "intent_id": str(UUID(int=number)),
            "owner_id": OWNER,
            "conversation_id": str(UUID(int=703)),
            "channel_instance": CHANNEL,
            **fields,
        }
        bot = RecordingBot()
        result = await TelegramChannel(bot, CHAT).deliver(
            OutboundIntent.model_validate(intent), now=NOW
        )
        results.append({"name": name, "intent": intent, "state": result.state, "calls": bot.calls})
    result = {"chat": CHAT, "owner": OWNER, "now": NOW.isoformat(), "cases": results}
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(capture(parser.parse_args().output))
