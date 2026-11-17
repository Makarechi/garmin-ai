"""Replay existing Telegram and web pause controls on a disposable database."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.config import Settings
from garmin_ai.models import (
    AppState,
    Conversation,
    Insight,
    OutboxMessage,
    PendingQuestion,
    Person,
    TelegramUpdate,
)
from garmin_ai.telegram import process_message, save_update
from garmin_ai.web_chat import _set_paused

SECOND = datetime(2026, 9, 7, 12, tzinfo=UTC)
KEY = "proactive:enabled"


def cases():
    def telegram(update, offset, micros, epoch, enabled):
        return {
            "lane": "telegram",
            "update_id": update,
            "ordering_epoch": epoch,
            "message_at": (SECOND + timedelta(seconds=offset)).isoformat(),
            "received_at": (SECOND + timedelta(seconds=offset, microseconds=micros)).isoformat(),
            "enabled": enabled,
        }

    def web(offset, micros, enabled):
        return {
            "lane": "web",
            "received_at": (SECOND + timedelta(seconds=offset, microseconds=micros)).isoformat(),
            "enabled": enabled,
        }

    return [
        web(0, 500_000, False),
        telegram(10, 0, 200_000, 0, True),
        telegram(11, 0, 700_000, 0, True),
        web(0, 600_000, False),
        web(0, 800_000, False),
        telegram(9, 0, 900_000, 0, True),
        telegram(101, 1, 200_000, 0, False),
        telegram(100, 1, 900_000, 0, True),
        telegram(8, 1, 100_000, 1, True),
        telegram(999999, 1, 900_000, 0, False),
        telegram(7, 2, 500_000, 2, False),
        web(2, 550_000, True),
        telegram(6, 2, 900_000, 2, False),
        telegram(5, 3, 100_000, 3, False),
    ]


def seed(session):
    owner = session.scalar(select(Person.id))
    conversation = Conversation(
        owner_id=owner,
        channel="web",
        channel_instance_id="primary",
        external_conversation_id="synthetic-proactive-fixture",
    )
    session.add(conversation)
    session.flush()
    for key, state, initiative in [
        ("initiative", "queued", True),
        ("ordinary", "queued", False),
        ("uncertain", "uncertain", True),
    ]:
        session.add(
            OutboxMessage(
                owner_id=owner,
                conversation_id=conversation.id,
                operation_id=uuid4(),
                intent={"initiative": initiative},
                state=state,
                next_attempt_at=SECOND if state == "queued" else None,
                dedup_key=f"fixture:{key}",
            )
        )
    for key, sent in [("pending", None), ("sending", SECOND), ("sent", SECOND)]:
        session.add(
            PendingQuestion(
                kind="migraine",
                text="synthetic",
                evidence={},
                priority=1,
                earliest_send_at=SECOND,
                expires_at=SECOND + timedelta(days=1),
                sent_at=sent,
                status=key,
                dedup_key=f"fixture:{key}",
            )
        )
    for status in ["accepted", "candidate"]:
        session.add(
            Insight(
                category="synthetic",
                statement="synthetic",
                evidence={},
                sample_size=5,
                status=status,
                dedup_key=f"fixture:{status}",
            )
        )
    session.add(AppState(key="insight:last:reservation", value={"reservation": "synthetic"}))
    session.add(AppState(key="insight:last:retained", value={"reservation": False}))
    session.commit()


def snapshot(session):
    return {
        "policy": session.get(AppState, KEY).value,
        "outbox": {
            row.dedup_key: [row.state, row.next_attempt_at is not None]
            for row in session.scalars(
                select(OutboxMessage).where(OutboxMessage.dedup_key.startswith("fixture:"))
            )
        },
        "questions": {
            row.dedup_key: [row.status, row.sent_at is not None, row.evidence.get("cancel_reason")]
            for row in session.scalars(
                select(PendingQuestion).where(PendingQuestion.dedup_key.startswith("fixture:"))
            )
        },
        "insights": {
            row.dedup_key: [row.status, row.evidence.get("cancel_reason")]
            for row in session.scalars(
                select(Insight).where(Insight.dedup_key.startswith("fixture:"))
            )
        },
        "reservations": sorted(
            session.scalars(select(AppState.key).where(AppState.key.startswith("insight:last:")))
        ),
    }


def run(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session:
            seed(session)
            results = []
            for case in cases():
                previous = session.get(AppState, KEY)
                old = previous.value.copy() if previous else {}
                if case["lane"] == "web":
                    _set_paused(
                        session, case["enabled"], datetime.fromisoformat(case["received_at"])
                    )
                    session.commit()
                else:
                    update = case["update_id"]
                    payload = {
                        "update_id": update,
                        "message": {
                            "message_id": update,
                            "date": int(datetime.fromisoformat(case["message_at"]).timestamp()),
                            "from": {"id": 42},
                            "chat": {"id": 42, "type": "private"},
                            "text": "/resume" if case["enabled"] else "/pause",
                        },
                    }
                    assert save_update(session, payload, 42)
                    row = session.get(TelegramUpdate, update)
                    row.received_at = datetime.fromisoformat(case["received_at"])
                    row.payload = {**row.payload, "_ordering_epoch": case["ordering_epoch"]}
                    session.commit()
                    process_message(engine, None, Settings(telegram_user_id=42), update)
                session.expire_all()
                state = snapshot(session)
                results.append({**case, "changed": old != state["policy"], "state": state})
            output.write_text(json.dumps(results, ensure_ascii=False))
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)


if __name__ == "__main__":
    main()
