from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from garmin_ai.accounts import bind_account, profile_fingerprint
from garmin_ai.activity_sync import current_page, finish_page, scan_status, schedule_scans
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.models import Activity, AppState, Job
from garmin_ai.sync import run_garmin_job

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
ACCOUNT = profile_fingerprint({"profileId": 12345})


def activity(index):
    return {
        "activityId": index,
        "startTimeGMT": (NOW - timedelta(hours=index)).isoformat(),
        "duration": 1800,
    }


@pytest.mark.parametrize("insert_mid_scan", [False, True])
def test_more_than_two_hundred_activities_survive_restart_and_offset_shift(
    db, db_engine, tmp_path, insert_mid_scan
):
    bind_account(db, ACCOUNT)
    settings = Settings(backfill_days=0, timezone="UTC")
    schedule_scans(db, settings, NOW)
    db.commit()
    values = [activity(index) for index in range(1, 251)]
    calls = []

    def fetch(method, offset, limit):
        assert method == "get_activities"
        calls.append(offset)
        result = values[offset : offset + limit]
        if insert_mid_scan and len(calls) == 1:
            values.insert(0, activity(0))
        return result

    reader = SimpleNamespace(account_fingerprint=lambda: ACCOUNT, call=fetch)
    for _ in range(10):
        db.expire_all()
        job = db.scalar(
            select(Job)
            .where(Job.kind == "garmin_activities", Job.status == "pending")
            .order_by(Job.run_at)
        )
        if not job:
            break
        payload = dict(job.payload)
        db.commit()
        run_garmin_job(
            db_engine, reader, LocalArchive(tmp_path), settings, "garmin_activities", payload
        )
        db.expire_all()
        job.status = "done"
        db.commit()
    else:
        pytest.fail("Scan did not terminate")
    assert db.scalar(select(func.count()).select_from(Activity)) == len(values)
    assert scan_status(db)[0]["status"] == "complete"
    assert calls == ([0, 80, 0, 80, 160] if insert_mid_scan else [0, 80, 160])
    fits = db.scalars(select(Job).where(Job.kind == "garmin_fit")).all()
    assert len(fits) == len(values)
    assert all(row.payload["account"] == ACCOUNT for row in fits)


def test_scheduling_coalesces_active_scan_and_new_generation_refreshes_children(db):
    bind_account(db, ACCOUNT)
    settings = Settings(backfill_days=365, timezone="UTC")
    schedule_scans(db, settings, NOW)
    schedule_scans(db, settings, NOW + timedelta(minutes=15))
    jobs = db.scalars(select(Job)).all()
    assert len(jobs) == 2
    recent = next(job for job in jobs if not job.payload["backfill"])
    assert finish_page(db, recent.payload, [], "UTC", NOW)
    schedule_scans(db, settings, NOW + timedelta(minutes=16))
    jobs = db.scalars(select(Job)).all()
    assert len(jobs) == 3
    assert len({job.payload["generation"] for job in jobs}) == 3
    assert not current_page(db, recent.payload)


def test_repeated_inventory_shifts_stop_with_visible_incomplete_status(db):
    bind_account(db, ACCOUNT)
    schedule_scans(db, Settings(backfill_days=0), NOW)
    row = db.scalar(select(AppState).where(AppState.key.startswith("activity_scan:")))
    for attempt in range(4):
        payload = {**row.value, "round": attempt, "offset": 80, "anchor": ["missing"]}
        row.value = payload
        db.flush()
        finish_page(db, payload, [activity(1)], "UTC", NOW)
    assert row.value["status"] == "unstable_inventory"
    assert row.value["next_scan_at"] is not None
    assert "account" not in scan_status(db)[0]
