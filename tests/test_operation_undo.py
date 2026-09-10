from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.event_batches import DraftLink, create_batch
from garmin_ai.events import Conflict, EventInput, create_event, undo_last, update_event
from garmin_ai.models import Audit, Event

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def batch(db):
    return create_batch(
        db,
        [
            EventInput(start=NOW, payload={"type": "caffeine", "beverage": "synthetic"}),
            EventInput(start=NOW + timedelta(hours=1), payload={"type": "migraine"}),
            EventInput(
                start=NOW + timedelta(hours=2),
                payload={"type": "medication", "name": "synthetic", "dose": 1, "unit": "tablet"},
            ),
        ],
        [DraftLink(child_index=2, parent_index=1)],
        actor="owner",
        update_id=1,
    )


def test_undo_removes_entire_linked_operation_and_is_not_repeatable(db):
    rows = batch(db)
    db.flush()
    identities = set(db.scalars(select(Audit.operation_id)))
    assert len(identities) == 1 and None not in identities
    undo_last(db, actor="owner")
    assert all(row.deleted for row in rows)
    assert db.info["undo_count"] == 3
    assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "undo")) == 3
    with pytest.raises(Conflict):
        undo_last(db, actor="owner")


def test_newer_other_channel_edit_prevents_partial_undo(db):
    rows = batch(db)
    parent = rows[1]
    update_event(
        db,
        parent.id,
        EventInput(start=parent.start, payload={"type": "migraine", "severity": 5}),
        revision=parent.revision,
        actor="other-channel",
    )
    db.flush()
    with pytest.raises(Conflict):
        undo_last(db, actor="owner")
    db.expire_all()
    assert all(not db.get(Event, row.id).deleted for row in rows)
    assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "undo")) == 0


def test_external_child_reference_rolls_back_operation_undo(db):
    rows = batch(db)
    create_event(
        db,
        EventInput(
            start=NOW + timedelta(hours=3),
            payload={
                "type": "medication",
                "name": "synthetic other",
                "dose": 1,
                "unit": "tablet",
                "reason_event_id": rows[1].id,
            },
        ),
        actor="other-channel",
    )
    db.flush()
    with pytest.raises(Conflict):
        undo_last(db, actor="owner")
    db.expire_all()
    assert all(not db.get(Event, row.id).deleted for row in rows)


def test_legacy_individual_mutations_keep_individual_undo(db):
    first = create_event(
        db, EventInput(start=NOW, payload={"type": "note", "description": "first"}), actor="owner"
    )
    last = create_event(
        db, EventInput(start=NOW, payload={"type": "note", "description": "last"}), actor="owner"
    )
    db.flush()
    undo_last(db, actor="owner")
    assert last.deleted and not first.deleted
    assert db.info["undo_count"] == 1


def test_previous_schema_export_restores_without_operation_identity(db, db_engine, tmp_path):
    import gzip
    import json

    from sqlalchemy import text

    from garmin_ai.models import Base
    from garmin_ai.operations import export_database, restore_database

    event = create_event(db, EventInput(start=NOW, payload={"type": "migraine"}), actor="owner")
    identity = event.id
    db.commit()
    archive = tmp_path / "legacy.gz"
    counts = export_database(db_engine, archive)
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    records[0]["revision"] = "a637902bf114"
    for record in records:
        if record.get("table") == "audit_log":
            record["row"].pop("operation_id", None)
    with gzip.open(archive, "wt", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")
    names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    assert restore_database(db_engine, archive) == counts
    db.expire_all()
    audit = db.scalar(select(Audit).where(Audit.event_id == identity))
    assert audit is not None and audit.operation_id is None
