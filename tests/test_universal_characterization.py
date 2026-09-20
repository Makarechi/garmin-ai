"""UNI-01 snapshots of legacy behavior that later universalization must preserve."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from garmin_ai.config import Settings
from garmin_ai.conversation import (
    conversation_context,
    forget_conversation,
    remember_answer,
)
from garmin_ai.event_batches import DraftLink, create_batch
from garmin_ai.events import EventInput, undo_last
from garmin_ai.models import AppState, Audit, Event, Job, TelegramUpdate
from garmin_ai.queries import list_events
from garmin_ai.telegram import DeliveryUncertain, deliver, process_message, save_update

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def telegram_update(identity, text):
    return {
        "update_id": identity,
        "message": {
            "message_id": identity,
            "date": int(NOW.timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": text,
        },
    }


def test_legacy_linked_batch_retry_open_interval_and_undo_are_stable(db):
    migraine = EventInput(start=NOW, timezone="UTC", payload={"type": "migraine"})
    unknown_medication = EventInput(
        start=NOW + timedelta(minutes=20),
        timezone="UTC",
        payload={"type": "medication"},
    )
    link = DraftLink(child_index=1, parent_index=0)

    first = create_batch(
        db,
        [migraine, unknown_medication],
        [link],
        actor="telegram:42",
        update_id=7001,
    )
    replay = create_batch(
        db,
        [migraine, unknown_medication],
        [link],
        actor="telegram:42",
        update_id=7001,
    )

    assert [row.id for row in replay] == [row.id for row in first]
    parent, medication = first
    assert medication.payload == {
        "type": "medication",
        "name": None,
        "dose": None,
        "unit": None,
        "reason_event_id": str(parent.id),
    }
    assert db.scalar(select(func.count()).select_from(Event)) == 2
    assert db.scalar(select(func.count()).select_from(Audit)) == 2

    next_day = list_events(db, NOW + timedelta(hours=12), NOW + timedelta(days=1, hours=12))
    assert [(row["id"], row["topology"], row["end"]) for row in next_day["rows"]] == [
        (str(parent.id), "open_interval", None)
    ]

    undo_last(db, actor="telegram:42")
    db.flush()
    assert session_ids(db, Event.deleted.is_(True)) == {parent.id, medication.id}
    assert db.info["undo_count"] == 2
    assert db.scalars(select(Audit.action).order_by(Audit.id)).all() == [
        "create",
        "create",
        "undo",
        "undo",
    ]


def session_ids(db, predicate):
    return set(db.scalars(select(Event.id).where(predicate)).all())


def test_duplicate_ingress_cancel_and_uncertain_delivery_keep_single_effect(db, db_engine):
    db.add(AppState(key="conversation:pending", value={"question": "synthetic"}))
    update = telegram_update(8002, "/cancel")
    assert save_update(db, update, 42)
    assert save_update(db, update, 42)
    assert db.scalar(select(func.count()).select_from(TelegramUpdate)) == 1
    assert db.scalar(select(func.count()).select_from(Job)) == 1

    db.commit()
    response = process_message(db_engine, None, Settings(telegram_user_id=42), 8002)
    assert response == "Уточнение отменено. Можно добавить новую запись."
    with db_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(AppState)
                .where(AppState.key == "conversation:pending")
            )
            == 0
        )

    db.merge(
        AppState(
            key="outbox:proactive:synthetic:0",
            value={"status": "uncertain", "formatted": True},
        )
    )
    db.commit()

    class Bot:
        async def send_message(self, **kwargs):
            raise AssertionError("an uncertain delivery must not be sent again")

    try:
        asyncio.run(deliver(Bot(), db_engine, 42, "proactive:synthetic", "synthetic"))
    except DeliveryUncertain:
        pass
    else:
        raise AssertionError("uncertain delivery must stay explicit")


def test_forget_fences_stale_answer_and_uncertain_answer_stays_out_of_context(db):
    db.add(AppState(key="outbox:update:9001:0", value={"status": "uncertain", "message_id": 1}))
    remember_answer(
        db,
        NOW,
        9001,
        "Synthetic question",
        "Synthetic answer",
        [{"tool": "synthetic", "arguments": {}, "result": {"status": "ok"}}],
        epoch=None,
    )
    assert conversation_context(db, NOW)["turns"] == []

    old_epoch = conversation_context(db, NOW)["epoch"]
    forget_conversation(db)
    db.merge(AppState(key="outbox:update:9002:0", value={"status": "sent", "message_id": 2}))
    remember_answer(
        db,
        NOW,
        9002,
        "Stale question",
        "Stale answer",
        [],
        epoch=old_epoch,
    )
    assert conversation_context(db, NOW)["turns"] == []
