from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from garmin_ai.agent import Interpretation, apply_command
from garmin_ai.event_batches import DraftLink, create_batch
from garmin_ai.events import EventInput
from garmin_ai.models import Audit, Event

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def medication(**extra):
    return EventInput(
        start=NOW + timedelta(minutes=20),
        payload={"type": "medication", "name": "synthetic", "dose": 1, "unit": "tablet", **extra},
    )


def migraine():
    return EventInput(start=NOW, payload={"type": "migraine"})


def test_child_before_parent_is_linked_and_retry_is_idempotent(db):
    command = Interpretation(
        intent="log",
        confidence=1,
        events=[medication(), migraine()],
        draft_links=[DraftLink(child_index=0, parent_index=1)],
    )
    for _ in range(2):
        apply_command(
            db,
            command,
            text="synthetic batch",
            update_id=1,
            actor="owner",
            now=NOW + timedelta(hours=1),
        )
    rows = db.scalars(select(Event)).all()
    assert len(rows) == 2
    parent = next(row for row in rows if row.kind == "migraine")
    child = next(row for row in rows if row.kind == "medication")
    assert child.payload["reason_event_id"] == str(parent.id)
    assert child.idempotency_key == "telegram:1:0"
    assert db.scalar(select(func.count()).select_from(Audit)) == 2


def test_failure_of_one_draft_rolls_back_all_created_facts(db):
    with pytest.raises(ValueError):
        create_batch(
            db, [migraine(), medication(reason_event_id=uuid4())], [], actor="owner", update_id=1
        )
    db.commit()
    assert db.scalar(select(func.count()).select_from(Event)) == 0
    assert db.scalar(select(func.count()).select_from(Audit)) == 0


@pytest.mark.parametrize("links", [[(0, 0)], [(0, 2)], [(1, 0)], [(0, 1), (0, 1)]])
def test_invalid_local_links_rejected_before_writes(links):
    with pytest.raises(ValidationError):
        Interpretation(
            intent="log",
            confidence=1,
            events=[medication(), migraine()],
            draft_links=[DraftLink(child_index=a, parent_index=b) for a, b in links],
        )


def test_existing_relation_cannot_be_replaced_by_local_link():
    with pytest.raises(ValidationError):
        Interpretation(
            intent="log",
            confidence=1,
            events=[medication(reason_event_id=uuid4()), migraine()],
            draft_links=[DraftLink(child_index=0, parent_index=1)],
        )


def test_new_batch_requires_explicit_drug_and_dose():
    with pytest.raises(ValidationError):
        EventInput(start=NOW, payload={"type": "medication", "name": "synthetic"})
    with pytest.raises(ValidationError):
        DraftLink(child_index=True, parent_index=1)
