"""Capture the existing Telegram webhook route with synthetic Bot API updates."""

import argparse
import json
from pathlib import Path

from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.models import AppState

SECRET = "synthetic-webhook-secret-32-characters"
CHAT = 42
HEADER = "X-Telegram-Bot-Api-Secret-Token"


def capture(output):
    engine = isolated_engine()
    try:
        settings = Settings(telegram_webhook_secret=SECRET, telegram_user_id=CHAT)
        events = []
        message = {
            "message_id": 761,
            "from": {"id": CHAT, "is_bot": False, "first_name": "Synthetic"},
            "chat": {"id": CHAT, "type": "private"},
            "date": 1794916800,
            "text": "synthetic Telegram message",
        }
        valid = {"update_id": 461, "message": message}
        with TestClient(create_app(settings, engine), raise_server_exceptions=False) as client:

            def request(name, *, payload=valid, secret=SECRET, large=False):
                response = client.post(
                    "/telegram/webhook",
                    headers={} if secret is None else {HEADER: secret},
                    **({"content": b"x" * (1024 * 1024 + 1)} if large else {"json": payload}),
                )
                events.append(
                    {
                        "name": name,
                        "payload": payload,
                        "auth": "valid"
                        if secret == SECRET
                        else "missing"
                        if secret is None
                        else "wrong",
                        "large": large,
                        "status": response.status_code,
                        "body": response.json(),
                    }
                )

            request("missing_secret", secret=None)
            request("wrong_secret_first", secret="fixture-wrong", large=True)
            request("too_large", large=True)
            request("accepted")
            request("duplicate")
            request(
                "foreign",
                payload={
                    "update_id": 462,
                    "message": {**message, "from": {**message["from"], "id": CHAT + 1}},
                },
            )
            request(
                "edit",
                payload={"update_id": 463, "edited_message": {**message, "text": "synthetic edit"}},
            )
            with engine.connect() as connection, connection.begin():
                connection.execute(text("SELECT pg_advisory_xact_lock(72104623)"))
                request("busy", payload={**valid, "update_id": 464})
            request("replay_after_busy", payload={**valid, "update_id": 464})
            with Session(engine) as session, session.begin():
                session.add(
                    AppState(
                        key="preferences:onboarding",
                        value={"channel": None, "fallback_channels": []},
                    )
                )
            request("disabled", payload={**valid, "update_id": 465})
        output.write_text(json.dumps({"chat": CHAT, "cases": events}), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
