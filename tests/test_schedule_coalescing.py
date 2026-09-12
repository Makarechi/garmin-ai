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
