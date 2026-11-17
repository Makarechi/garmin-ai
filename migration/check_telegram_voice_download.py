"""Record pinned Telegram voice download requests without a bot connection."""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from telegram import Bot
from telegram.request import HTTPXRequest

TOKEN = "100:synthetic-transport"
FILE_ID = "synthetic_telegram_voice_id"


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
        elif method == "getFile":
            fields = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            captured.append({"method": request.method, "endpoint": method, "fields": fields})
            result = {"file_id": FILE_ID, "file_unique_id": "fixture", "file_size": 4}
            result["file_path"] = "voice/file_7.oga"
        else:
            captured.append({"method": request.method, "endpoint": request.url.path, "fields": {}})
            return httpx.Response(200, content=b"OggS")
        return httpx.Response(200, json={"ok": True, "result": result})

    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(handle)})
    async with Bot(TOKEN, request=request) as bot:
        file = await bot.get_file(FILE_ID)
        assert await file.download_as_bytearray() == b"OggS"
    assert len(captured) == 2
    output.write_text(json.dumps(captured), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(capture(parser.parse_args().output))
