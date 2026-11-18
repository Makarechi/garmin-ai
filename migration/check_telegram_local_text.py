"""Capture pinned Telegram responses for local emergency and unavailable-model text."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.accounts import bind_channel
from garmin_ai.config import Settings
from garmin_ai.telegram import process_message, save_update


def seed(engine):
    with Session(engine) as session:
        bind_channel(
            session,
            channel="telegram",
            channel_instance_id="primary",
            external_id="42",
            confirmed=True,
        )
        session.commit()


def capture(engine):
    seed(engine)
    voice = {
        "file_id": "synthetic-audio-reference",
        "file_unique_id": "synthetic-audio-unique-reference",
        "duration": 1,
        "mime_type": "audio/ogg",
        "file_size": 128,
    }
    expected = {}
    for update_id, kind, body in (
        (102, "text", {"text": "Synthetic ordinary report"}),
        (103, "emergency", {"voice": voice, "caption": "I can't breathe"}),
        (104, "voice", {"voice": voice}),
    ):
        update = {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                **body,
            },
        }
        with Session(engine) as session:
            assert save_update(session, update, 42)
            session.commit()
        expected[kind] = process_message(
            engine, None, Settings(telegram_user_id=42, locale="en"), update_id
        )
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        args.output.write_text(json.dumps(capture(engine), ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
