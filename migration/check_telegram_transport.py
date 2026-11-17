"""Record pinned Telegram polling and acknowledgement requests with an in-process API."""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from telegram import Bot
from telegram.request import HTTPXRequest


async def capture(output):
    captured = []

    def handle(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            result = {
                "id": 100,
                "is_bot": True,
                "first_name": "Synthetic",
                "username": "synthetic_bot",
            }
            return httpx.Response(200, json={"ok": True, "result": result})
        query = parse_qs(request.content.decode(), keep_blank_values=True)
        fields = {key: values[0] for key, values in query.items()}
        for key in ("timeout", "offset", "allowed_updates"):
            if key in fields:
                fields[key] = json.loads(fields[key])
        captured.append({"method": request.method, "endpoint": method, "fields": fields})
        result = [] if method == "getUpdates" else True
        return httpx.Response(200, json={"ok": True, "result": result})

    def request():
        return HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(handle)})

    async with Bot(
        "100:synthetic-transport", request=request(), get_updates_request=request()
    ) as bot:
        for offset in (None, 107):
            await bot.get_updates(
                offset=offset,
                timeout=15,
                allowed_updates=["message", "edited_message", "callback_query"],
            )
        await bot.answer_callback_query("synthetic/ack")

    assert len(captured) == 3
    output.write_text(json.dumps(captured), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(capture(parser.parse_args().output))
