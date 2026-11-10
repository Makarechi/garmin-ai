from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from garmin_ai.config import Settings
from garmin_ai.models import Job
from garmin_ai.sync import FREQUENT, schedule_sync


@pytest.mark.parametrize("hour", [3, 8, 18])
def test_overlapping_schedules_enqueue_one_current_day_fetch(db, hour):
    now = datetime(2026, 9, 7, hour, tzinfo=UTC)
    settings = Settings(timezone="UTC", backfill_days=0)
    for _ in range(2):
        schedule_sync(db, settings, now)
    jobs = db.scalars(select(Job).where(Job.kind == "garmin_endpoint")).all()
    current = Counter(row.payload["endpoint"] for row in jobs if row.payload["key"] == "2026-09-07")
    assert current and max(current.values()) == 1
    assert FREQUENT <= current.keys()
    if hour == 3:
        assert any(row.payload["key"] == "2026-09-06" for row in jobs)
        assert any(row.payload["key"] == "2026-08-09" for row in jobs)
    if hour == 8:
        assert {"sleep", "hrv", "readiness"} <= current.keys()
    if hour == 18:
        assert "hydration" in current


def test_completed_frequent_slot_does_not_disable_the_next_refresh(db):
    settings = Settings(timezone="UTC", backfill_days=0)
    schedule_sync(db, settings, datetime(2026, 9, 7, 8, tzinfo=UTC))
    first = db.scalars(select(Job).where(Job.kind == "garmin_endpoint")).all()
    for row in first:
        row.status = "done"
    db.flush()
    schedule_sync(db, settings, datetime(2026, 9, 7, 8, 15, tzinfo=UTC))
    pending = db.scalars(
        select(Job).where(Job.kind == "garmin_endpoint", Job.status == "pending")
    ).all()
    assert {row.payload["endpoint"] for row in pending} >= FREQUENT
    assert sum(row.payload["endpoint"] == "readiness" for row in pending) == 1


@pytest.mark.parametrize("day, days", [(7, 30), (8, 7)])
def test_nightly_endpoint_schedule_batches_writes_and_refreshes_policy(db, monkeypatch, day, days):
    from datetime import timedelta

    from sqlalchemy import event

    from garmin_ai import activity_sync, backfill, scenario_packs
    from garmin_ai.garmin_contract import ENDPOINTS

    now = datetime(2026, 9, day, 3, tzinfo=UTC)
    settings = Settings(timezone="UTC", backfill_days=0)
    allow_stress = False
    checked = Counter()

    def enabled(_session, endpoint):
        checked[endpoint] += 1
        return endpoint != "stress" or allow_stress

    monkeypatch.setattr(backfill, "schedule_history", lambda *_args: None)
    monkeypatch.setattr(activity_sync, "schedule_scans", lambda *_args: True)
    monkeypatch.setattr(scenario_packs, "garmin_collection_enabled", enabled)
    writes = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO jobs"):
            writes.append(statement)

    event.listen(db.connection(), "before_cursor_execute", capture)
    try:
        schedule_sync(db, settings, now)
    finally:
        event.remove(db.connection(), "before_cursor_execute", capture)
    expected = {
        (endpoint.name, str(now.date() - timedelta(days=offset)))
        for endpoint in ENDPOINTS
        if endpoint.scope == "day" and endpoint.name != "stress"
        for offset in range(days)
    } | {
        (endpoint.name, "global")
        for endpoint in ENDPOINTS
        if endpoint.scope == "global" and endpoint.name != "stress"
    }
    rows = list(db.scalars(select(Job).where(Job.kind == "garmin_endpoint")))
    assert {(row.payload["endpoint"], row.payload["key"]) for row in rows} == expected
    assert len(rows) == len(expected)
    assert len({row.id for row in rows}) == len(rows) and all(row.id is not None for row in rows)
    assert len(writes) == (len(expected) + 499) // 500
    assert max(checked.values()) == 1
    first = rows[0]
    first.status = "done"
    db.flush()
    checked.clear()
    allow_stress = True
    schedule_sync(db, settings, now)
    db.expire_all()
    assert db.get(Job, first.id).status == "done"
    stress = list(
        db.scalars(
            select(Job).where(
                Job.kind == "garmin_endpoint", Job.payload["endpoint"].as_string() == "stress"
            )
        )
    )
    assert len(stress) == days and {row.payload["key"] for row in stress} == {
        str(now.date() - timedelta(days=offset)) for offset in range(days)
    }
    assert max(checked.values()) == 1
