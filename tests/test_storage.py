from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from garmin_ai.events import (
    Conflict,
    EventInput,
    create_event,
    delete_event,
    undo_last,
    update_event,
)
from garmin_ai.jobs import claim, enqueue, finish
from garmin_ai.models import Audit, Event


def coffee():
    return EventInput(
        start="2026-09-07T11:00:00+02:00", payload={"type": "caffeine", "beverage": "espresso"}
    )


def test_event_replay_edit_conflict_and_undo(db):
    event = coffee()
    first = create_event(db, event, actor="owner", idempotency_key="telegram:1")
    again = create_event(db, event, actor="owner", idempotency_key="telegram:1")
    assert first.id == again.id
    assert db.scalar(select(func.count()).select_from(Event)) == 1
    assert db.scalar(select(func.count()).select_from(Audit)) == 1
    correction = event.model_copy(update={"start": datetime(2026, 9, 7, 8, 20, tzinfo=UTC)})
    with pytest.raises(Conflict):
        create_event(db, correction, actor="owner", idempotency_key="telegram:1")
    update_event(db, first.id, correction, revision=1, actor="owner")
    with pytest.raises(Conflict):
        update_event(db, first.id, event, revision=1, actor="owner")
    restored = undo_last(db, actor="owner")
    assert restored.start == event.start
    assert restored.revision == 3
    with pytest.raises(Conflict):
        undo_last(db, actor="owner")
    delete_event(db, first.id, revision=3, actor="owner")
    assert first.deleted
    assert not undo_last(db, actor="owner").deleted


@pytest.mark.parametrize(
    "changes",
    [
        {"start": "2026-09-07T11:00:00"},
        {"end": "2026-09-06T11:00:00Z"},
        {"payload": {"type": "migraine", "severity": 11}},
        {"payload": {"type": "medication", "dose": 50, "unit": "mg"}},
        {
            "payload": {
                "type": "caffeine",
                "beverage": "coffee",
                "caffeine_mg_min": 150,
                "caffeine_mg_max": 100,
            }
        },
        {"confidence": float("nan")},
        {"source": "inferred", "status": "confirmed"},
    ],
)
def test_invalid_event_never_reaches_storage(changes):
    data = coffee().model_dump()
    with pytest.raises((ValidationError, ValueError)):
        EventInput.model_validate({**data, **changes})


def test_job_replay_lease_recovery_and_stale_ack(db, db_engine):
    now = datetime.now(UTC)
    job_id = enqueue(db, "sync", {"day": "2026-09-07"}, "sync:day", now)
    assert enqueue(db, "sync", {}, "sync:day", now) is None
    db.commit()
    first = claim(db, now=now, lease_seconds=1)
    old_token = first.lease_token
    db.commit()
    with Session(db_engine) as other:
        assert claim(other, now=now) is None
        recovered = claim(other, now=now + timedelta(seconds=2))
        assert recovered.id == job_id
        new_token = recovered.lease_token
        other.commit()
    db.expire_all()
    with pytest.raises(ValueError):
        finish(db, job_id, old_token)
    finish(db, job_id, new_token)
    assert first.status == "done"


def test_timescale_migration_is_real(db):
    assert (
        db.scalar(
            text(
                "SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name = 'measurements'"
            )
        )
        == 1
    )
