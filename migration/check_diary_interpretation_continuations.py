"""Record pinned diary continuation context on disposable PostgreSQL."""

import argparse
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from check_diary_interpretation_events import add_history
from check_model_diary_policy import END as NOW
from check_model_diary_policy import seed
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.agent import context_for
from garmin_ai.models import AppState, Conversation, PendingQuestion, Person

CHANNEL = "telegram:allowed"
CONVERSATION = UUID(int=901)
OLD = NOW - timedelta(minutes=15)
NEW = NOW - timedelta(minutes=5)


def seed_continuations(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id))
        session.add(
            Conversation(
                id=CONVERSATION,
                owner_id=owner,
                channel="telegram",
                channel_instance_id="allowed",
                external_conversation_id="42",
                state={
                    "analysis_turns": [
                        {
                            "operation_id": str(UUID(int=900)),
                            "question": "Older native question",
                            "asked_at": (NOW - timedelta(minutes=10)).isoformat(),
                        },
                        {
                            "operation_id": str(UUID(int=899)),
                            "question": "Expired native question",
                            "asked_at": (NOW - timedelta(days=8)).isoformat(),
                        },
                    ]
                },
            )
        )
        old = dict(
            update_id="801",
            question="Delivered legacy question",
            channel_instance_id=CHANNEL,
            asked_at=OLD.isoformat(),
        )
        new = dict(
            update_id="802",
            question="New pending legacy question",
            channel_instance_id=CHANNEL,
            asked_at=NEW.isoformat(),
        )
        epoch = str(UUID(int=822))
        session.add_all(
            [
                AppState(key="analysis:conversation", value={"epoch": epoch, "turns": [old]}),
                AppState(
                    key=f"analysis:conversation:pending:{CHANNEL}",
                    value={"epoch": epoch, "turn": new},
                ),
                AppState(
                    key="outbox:update:801:0",
                    value={"status": "sent", "channel_instance_id": CHANNEL},
                ),
                AppState(
                    key="outbox:update:802:0",
                    value={"status": "sent", "channel_instance_id": CHANNEL},
                ),
                AppState(
                    key=f"conversation:pending:{CHANNEL}",
                    value={
                        "action": "update",
                        "event_ids": [str(UUID(int=1))],
                        "created_at": OLD.isoformat(),
                        "channel_instance_id": CHANNEL,
                    },
                ),
            ]
        )
        for number, kind, status, target, expires in [
            (830, "migraine", "sent", 120, True),
            (831, "migraine", "uncertain", 1, True),
            (832, "migraine", "sent", 450, True),
            (833, "migraine", "pending", 120, True),
            (834, "migraine", "sent", 120, False),
            (835, "caffeine", "sent", None, True),
            (836, "context", "acknowledged", None, True),
        ]:
            session.add(
                PendingQuestion(
                    id=UUID(int=number),
                    kind=kind,
                    status=status,
                    event_id=UUID(int=target) if target is not None else None,
                    text=f"Synthetic question {number}",
                    evidence={},
                    dedup_key=f"synthetic:{number}",
                    priority=0.7,
                    earliest_send_at=OLD - timedelta(minutes=5),
                    sent_at=OLD + timedelta(seconds=number - 830),
                    expires_at=NOW + timedelta(days=1) if expires else NOW - timedelta(seconds=1),
                )
            )


def capture(output):
    engine = isolated_engine()
    try:
        seed(engine)
        add_history(engine)
        seed_continuations(engine)
        with Session(
            engine,
            info={
                "model_provider_instance_id": "model:primary",
                "channel_destination_instance_id": CHANNEL,
            },
        ) as session:
            context = context_for(session, NOW, maintain=False)
            for event in context["recent_events"]:
                event.pop("original_text", None)
            assert [question["id"] for question in context["recent_questions"]] == [
                str(UUID(int=831)),
                str(UUID(int=830)),
            ]
            assert context["recent_analysis_question"]["question"] == "New pending legacy question"
            assert context["pending_clarification"]["event_ids"] == [str(UUID(int=1))]
        output.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
