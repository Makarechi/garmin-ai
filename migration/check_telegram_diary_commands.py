"""Compare local Telegram cancellation and grouped undo with pinned Python."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.accounts import bind_channel
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event, update_event
from garmin_ai.models import AppState, Event
from garmin_ai.telegram import process_message, save_update


def note(now, label):
    return EventInput(
        start=now,
        timezone="UTC",
        payload={"type": "note", "description": label},
    )


def seed(engine):
    now = datetime.now(UTC) - timedelta(minutes=1)
    with Session(engine) as session:
        bind_channel(
            session,
            channel="telegram",
            channel_instance_id="primary",
            external_id="42",
            confirmed=True,
        )
        session.add_all(
            [
                AppState(
                    key="tracker:chat-setup:telegram:primary",
                    value={
                        "locale": "en",
                        "step": "name",
                        "started_at": now.isoformat(),
                        "last_activity_at": now.isoformat(),
                        "started_provider_update_id": 898,
                        "last_provider_update_id": 902,
                        "started_ordering_epoch": 0,
                        "last_ordering_epoch": 0,
                    },
                ),
                AppState(
                    key="conversation:pending",
                    value={
                        "text": "Synthetic pending clarification",
                        "question": "Synthetic question",
                        "channel_instance_id": "telegram:primary",
                        "created_at": now.isoformat(),
                    },
                ),
                AppState(key="conversation:pending:web:local", value={"text": "Synthetic web"}),
            ]
        )
        create_event(session, note(now, "Synthetic API note"), actor="api")
        group = uuid4()
        for label in ("Synthetic grouped note one", "Synthetic grouped note two"):
            create_event(session, note(now, label), actor="telegram:42", operation_id=group)
        session.commit()


def seed_conflict(engine):
    now = datetime.now(UTC)
    with Session(engine) as session:
        group = uuid4()
        first = create_event(
            session, note(now, "Synthetic changed note"), actor="telegram:42", operation_id=group
        )
        create_event(
            session,
            note(now, "Synthetic unchanged group peer"),
            actor="telegram:42",
            operation_id=group,
        )
        update_event(
            session, first.id, note(now, "Synthetic changed note"), revision=1, actor="api"
        )
        session.commit()


def send(engine, update_id, message):
    wire = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": message,
        },
    }
    with Session(engine) as session:
        assert save_update(session, wire, 42)
        session.commit()
    return process_message(engine, None, Settings(telegram_user_id=42, timezone="UTC"), update_id)


def capture(engine):
    seed(engine)
    cases = [
        send(engine, 901, "/cancel"),
        send(engine, 903, "/cancel"),
        send(engine, 905, "/cancel"),
        send(engine, 907, "/undo"),
    ]
    seed_conflict(engine)
    cases.append(send(engine, 909, "/undo"))
    with Session(engine) as session:
        rows = session.scalars(select(Event)).all()
        events = {event.payload["description"]: [event.revision, event.deleted] for event in rows}
        assert events["Synthetic changed note"] == [2, False]
        assert events["Synthetic unchanged group peer"] == [1, False]
        assert session.get(AppState, "conversation:pending:web:local") is not None
        assert session.get(AppState, "conversation:pending") is None
        assert session.get(AppState, "tracker:chat-setup:telegram:primary") is None
    return {"replies": cases, "events": events}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, help="Write the frozen Python outcomes.")
    parser.add_argument(
        "--prepare-native",
        choices=("ordered", "conflict"),
        help="Seed a fresh test database for one native case using the same Python fixtures.",
    )
    args = parser.parse_args()
    if (args.output is None) == (args.prepare_native is None):
        parser.error("choose an output path or --prepare-native")
    engine = isolated_engine()
    try:
        if args.prepare_native is None:
            args.output.write_text(json.dumps(capture(engine), ensure_ascii=False))
        else:
            seed(engine)
            if args.prepare_native == "conflict":
                seed_conflict(engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
