"""Capture synthetic sendMessage requests from the pinned Telegram SDK and channel."""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

import httpx
from telegram import Bot
from telegram.request import HTTPXRequest

from garmin_ai.channels import DeliveryState, OutboundIntent
from garmin_ai.telegram_adapter import TelegramChannel

CHAT = 1234567890123


async def capture(output):
    captured = []

    def handle(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "getMe":
            response = {"id": 100, "is_bot": True, "first_name": "Synthetic", "username": "bot"}
        else:
            assert endpoint == "sendMessage"
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            form["chat_id"] = int(form["chat_id"])
            if "reply_markup" in form:
                form["reply_markup"] = json.loads(form["reply_markup"])
            captured.append({"method": request.method, "endpoint": endpoint, "fields": form})
            response = {
                "message_id": 700 + len(captured),
                "chat": {"id": CHAT, "type": "private"},
                "date": 1794916800,
            }
        return httpx.Response(200, json={"ok": True, "result": response})

    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(handle)})
    async with Bot("100:synthetic-transport", request=request) as bot:
        channel = TelegramChannel(bot, CHAT)
        intent = OutboundIntent.model_validate(
            {
                "owner_id": str(UUID(int=1001)),
                "conversation_id": str(UUID(int=1002)),
                "channel_instance": {"channel": "telegram", "instance_id": "primary"},
                "blocks": [{"text": "**literal** 1"}, {"text": "second 🧪"}, {"text": "third"}],
                "actions": [
                    {
                        "action_id": "confirm",
                        "label": "Confirm",
                        "operation_id": str(UUID(int=1003)),
                        "token": "synthetic_button_token_123",
                    },
                ],
            }
        )
        result = await channel.deliver(
            intent, now=datetime.fromisoformat("2026-11-17T12:00:00+00:00")
        )
        assert result.state is DeliveryState.PROVIDER_ACCEPTED
    assert len(captured) == 3
    output.write_text(json.dumps(captured, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(capture(parser.parse_args().output))
