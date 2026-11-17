"""Capture which accepted Telegram messages use the independent Python control lane."""

import argparse
import json
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.models import Job
from garmin_ai.telegram import save_update


def capture(output):
    variants = [
        ("ordinary", "synthetic diary update", False),
        ("forget", "/forget_conversation", False),
        ("today", "/today", False),
        ("status", "/status", False),
        ("debug", "/debug enable", False),
        ("goals", "/goals", False),
        ("pause", "\x1c/pause", False),
        ("resume", "/resume", False),
        ("help", "/help", False),
        ("start", "/start", False),
        ("voice_goal_listing", "/goals", True),
        ("voice_goal_arguments", "/goals synthetic", True),
        ("voice_caption", "/status", True),
        ("ordinary_in_slash_case", "/TODAY", False),
        ("mentioned_bot_suffix", "/today@synthetic_bot", False),
        ("history", "/history", False),
    ]
    engine = isolated_engine()
    cases = []
    try:
        with Session(engine) as session, session.begin():
            for index, (name, text, voice) in enumerate(variants, 501):
                message = {
                    "message_id": index + 100,
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "date": 1794916800,
                }
                if voice:
                    message.update(
                        caption=text, voice={"file_id": "synthetic-audio", "duration": 3}
                    )
                else:
                    message["text"] = text
                wire = {"update_id": index, "message": message}
                assert save_update(session, wire, 42)
                kind = session.scalar(select(Job.kind).where(Job.dedup_key == f"telegram:{index}"))
                assert kind in {"telegram_control", "telegram_update"}
                cases.append(
                    {
                        "name": name,
                        "wire": wire,
                        "lane": "control" if kind == "telegram_control" else "diary",
                    }
                )
    finally:
        engine.dispose()
    output.write_text(json.dumps(cases))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)


if __name__ == "__main__":
    main()
