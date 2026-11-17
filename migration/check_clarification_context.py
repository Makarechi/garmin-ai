"""Record Telegram and web clarification state using the existing diary commands."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.agent import Interpretation, apply_command, pending_clarification
from garmin_ai.models import AppState
from garmin_ai.pending_state import pending_key

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)
PRIMARY = "telegram:primary"
WEB = "web:primary"


def run(output):
    day = NOW.isoformat()
    seeded = {
        "conversation:pending": {
            "created_at": day,
            "text": "old report",
            "question": "old question",
            "button": "note",
            "action": "log",
            "event_ids": [],
            "preset_selected_at": day,
            "extra": "not propagated",
        },
        "conversation:pending:web:primary": {
            "created_at": day,
            "text": "existing web clarification",
            "question": "web question",
            "channel_instance_id": WEB,
            "action": "log",
        },
    }
    cases = [
        ("read", PRIMARY, 0, 0, False, None),
        ("read", PRIMARY, 7200, 7200, False, None),
        ("read", PRIMARY, 7201, 7201, False, None),
        ("read", PRIMARY, -10, 10, False, None),
        ("read", PRIMARY, -10, 10, True, None),
        ("read", WEB, 1, 1, False, None),
        ("read", "telegram:secondary", 1, 1, False, None),
        ("write", PRIMARY, 10, 12, False, "synthetic follow-up"),
        ("write", WEB, 20, 22, False, "synthetic web follow-up"),
    ]
    cases += [("write", PRIMARY, i, i, False, f"synthetic short text {i}") for i in range(30, 40)]
    cases += [
        ("write", PRIMARY, 42, 42, False, "🧪" * 8000),
        ("write", PRIMARY, 43, 43, False, "🧪" * 8000),
        ("write", "telegram:secondary", 44, 44, False, "synthetic secondary conversation"),
        ("read", PRIMARY, 7200, 7200, False, None),
        ("read", WEB, 7200, 7200, False, None),
        ("read", "telegram:secondary", 44, 44, False, None),
        ("write", WEB, 7300, 7300, False, "synthetic new web chain"),
    ]
    engine = isolated_engine()
    try:
        with Session(engine) as session:
            for key, value in seeded.items():
                session.add(AppState(key=key, value=value))
            session.commit()
            results = []
            for kind, destination, message, clock, use_message, text in cases:
                at = NOW + timedelta(seconds=message)
                now = NOW + timedelta(seconds=clock)
                session.info.update(
                    channel_destination_instance_id=destination, conversation_now=now
                )
                if kind == "write":
                    apply_command(
                        session,
                        Interpretation(
                            intent="clarify", confidence=1, clarification="synthetic question"
                        ),
                        text=text,
                        update_id=1,
                        actor="owner",
                        now=at,
                    )
                    target = session.get(AppState, pending_key(session), populate_existing=True)
                else:
                    target = pending_clarification(session, at, use_message_time=use_message)
                results.append(
                    {
                        "kind": kind,
                        "destination": destination,
                        "message_at": at.isoformat(),
                        "conversation_now": now.isoformat(),
                        "use_message_time": use_message,
                        "text": text,
                        "value": target.value if target else None,
                    }
                )
                session.commit()
            output.write_text(json.dumps({"seed": seeded, "cases": results}, ensure_ascii=False))
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)


if __name__ == "__main__":
    main()
