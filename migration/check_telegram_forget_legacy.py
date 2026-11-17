"""Capture the legacy Telegram analysis state cleared by /forget_conversation."""

import argparse
import json
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.conversation import forget_conversation
from garmin_ai.models import AppState

BEFORE = {
    "analysis:conversation": {
        "epoch": "11111111-1111-1111-1111-111111111111",
        "turns": [{"update_id": 51}],
    },
    "analysis:conversation:pending": {"turn": {"update_id": 52}},
    "analysis:conversation:pending:telegram:secondary": {"turn": {"update_id": 53}},
    "telegram:reply:51": {
        "text": "synthetic old linked analysis",
        "status": "sent",
        "keyboard": True,
    },
    "telegram:reply:52": {
        "text": "synthetic pending analysis",
        "status": "queued",
        "keyboard": True,
    },
    "telegram:reply:53": {
        "text": "synthetic secondary analysis",
        "status": "queued",
        "keyboard": True,
    },
    "telegram:reply:54": {
        "kind": "analysis",
        "text": "synthetic old answer",
        "status": "sent",
        "keyboard": True,
    },
    "telegram:reply:55": {
        "kind": "diary",
        "text": "synthetic saved diary notice",
        "status": "sent",
        "keyboard": False,
    },
    "other:synthetic": {"value": "untouched"},
}


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            session.add_all(AppState(key=key, value=value) for key, value in BEFORE.items())
            session.flush()
            forget_conversation(session)
            session.flush()
            rows = {
                row.key: row.value
                for row in session.scalars(
                    select(AppState)
                    .where(AppState.key.in_(BEFORE))
                    .execution_options(populate_existing=True)
                )
            }
            assert (
                rows["analysis:conversation"]["epoch"] != BEFORE["analysis:conversation"]["epoch"]
            )
            rows["analysis:conversation"]["epoch"] = "rotated"
            output.write_text(json.dumps({"before": BEFORE, "after": rows}, ensure_ascii=False))
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)


if __name__ == "__main__":
    main()
