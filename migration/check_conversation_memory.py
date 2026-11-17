"""Record neutral memory and forget behavior from the pinned Python dialogue service."""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.dialogue import DialogueService
from garmin_ai.models import Conversation, Person

NOW = datetime.fromisoformat("2026-11-17T12:00:00+00:00")
TARGET, SOURCE, UNRELATED = (str(UUID(int=value)) for value in (1201, 1202, 1203))


def turn(number, offset, *, source_epoch=None, wide=False):
    return {
        "operation_id": str(UUID(int=number)),
        "revision": 1,
        "asked_at": (NOW + offset).isoformat(),
        "question": ("🧪" * 1000) if wide else f"synthetic question {number}",
        "answer": ("я" * 1500) if wide else f"synthetic answer {number}",
        "question_truncated": False,
        "answer_truncated": False,
        "source_epochs": {SOURCE: source_epoch} if source_epoch else {},
    }


def initial():
    source_epoch = str(UUID(int=1212))
    return [
        {
            "id": TARGET,
            "epoch": str(UUID(int=1211)),
            "instance": "local",
            "channel": "web",
            "shared": False,
            "state": {
                "pending": {"synthetic": True},
                "analysis_turns": [
                    turn(1301, timedelta(days=-8)),
                    turn(1302, timedelta(days=-7, microseconds=-1)),
                    turn(1303, timedelta(days=-7)),
                    turn(1304, timedelta(hours=-1)),
                    turn(1305, timedelta(microseconds=1)),
                ],
            },
        },
        {
            "id": SOURCE,
            "epoch": source_epoch,
            "instance": "primary",
            "channel": "telegram",
            "shared": True,
            "state": {
                "analysis_turns": [
                    turn(1310, timedelta(hours=-10)),
                    turn(1311, timedelta(hours=-9)),
                    turn(1312, timedelta(hours=-8)),
                    turn(1313, timedelta(hours=-7)),
                    turn(1314, timedelta(hours=-6)),
                    turn(1315, timedelta(hours=-5)),
                    turn(1316, timedelta(hours=-4), wide=True),
                    turn(1317, timedelta(hours=-3), wide=True),
                ],
            },
        },
        {
            "id": UNRELATED,
            "epoch": str(UUID(int=1213)),
            "instance": "other",
            "channel": "telegram",
            "shared": False,
            "state": {"analysis_turns": [turn(1320, timedelta())]},
        },
    ]


def capture(path):
    db = isolated_engine()
    try:
        with Session(db) as session, session.begin():
            owner_id = session.scalar(select(Person.id))
            # Keep the seed unchanged as the live conversation objects are edited.
            seed = initial()
            for row in seed:
                session.add(
                    Conversation(
                        id=UUID(row["id"]),
                        owner_id=owner_id,
                        channel=row["channel"],
                        channel_instance_id=row["instance"],
                        share_owner_memory=row["shared"],
                        state=row["state"],
                        memory_epoch=UUID(row["epoch"]),
                    )
                )
            session.flush()
            service = DialogueService()
            events = []
            clocks = iter(UUID(int=i) for i in (1501, 1502, 1503))

            def rows():
                return [
                    {
                        "id": str(item.id),
                        "epoch": str(item.memory_epoch),
                        "shared": item.share_owner_memory,
                        "state": item.state,
                    }
                    for item in session.scalars(select(Conversation).order_by(Conversation.id))
                ]

            def apply(operation, identifier, *, data=None):
                result = None
                item = session.get(Conversation, UUID(identifier))
                if operation == "snapshot":
                    turns, epoch, sources = service.analysis_snapshot(session, item.id, NOW)
                    result = {
                        "turns": turns,
                        "target_epoch": str(epoch),
                        "source_epochs": {
                            str(source): str(value) for source, value in sources.items()
                        },
                    }
                elif operation == "sharing":
                    service.set_owner_memory_sharing(session, item.id, data)
                    result = str(item.memory_epoch)
                elif operation == "forget":
                    result = str(service.forget(session, item.id))
                elif operation == "append":
                    item.state = {
                        **item.state,
                        "analysis_turns": [*item.state.get("analysis_turns", []), data],
                    }
                    session.flush()
                events.append(
                    {
                        "operation": operation,
                        "id": identifier,
                        "data": data,
                        "result": result,
                        "rows": rows(),
                    }
                )

            with patch("garmin_ai.dialogue.uuid4", side_effect=clocks):
                apply("snapshot", TARGET)
                apply("sharing", TARGET, data=True)
                apply(
                    "append",
                    TARGET,
                    data=turn(1340, timedelta(minutes=-2), source_epoch=str(UUID(int=1212))),
                )
                apply(
                    "append",
                    TARGET,
                    data=turn(1341, timedelta(minutes=-1), source_epoch=str(UUID(int=1219))),
                )
                apply("snapshot", TARGET)
                apply("snapshot", UNRELATED)
                apply("forget", SOURCE)
                apply("snapshot", TARGET)
                apply(
                    "append",
                    TARGET,
                    data=turn(1342, timedelta(), source_epoch=str(UUID(int=1502))),
                )
                apply("sharing", SOURCE, data=False)
                apply("sharing", SOURCE, data=False)
                apply("snapshot", TARGET)
            path.write_text(
                json.dumps(
                    {"now": NOW.isoformat(), "seed": seed, "events": events},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    finally:
        db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
