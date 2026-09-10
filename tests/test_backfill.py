from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from garmin_ai.accounts import bind_account, profile_fingerprint
from garmin_ai.backfill import complete_window, history_status, schedule_history
from garmin_ai.config import Settings
from garmin_ai.garmin import ENDPOINTS
from garmin_ai.jobs import claim, enqueue
from garmin_ai.models import AppState, Job

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
ACCOUNT = profile_fingerprint({"profileId": 12345})
DAY_ENDPOINTS = sum(endpoint.scope == "day" for endpoint in ENDPOINTS)


def test_year_history_schedules_durably_without_manual_probe(db):
    bind_account(db, ACCOUNT)
    settings = Settings(backfill_days=365, timezone="UTC")
    for _ in range(183):
        schedule_history(db, settings, NOW)
        db.flush()
        db.expire_all()  # Simulate losing every in-memory cursor between passes.
    jobs = db.scalars(select(Job)).all()
    assert len(jobs) == 365 * DAY_ENDPOINTS
    assert min(row.payload["key"] for row in jobs) == "2025-09-10"
    assert max(row.payload["key"] for row in jobs) == "2026-09-09"
    schedule_history(db, settings, NOW)
    assert db.scalar(select(func.count()).select_from(Job)) == len(jobs)
    status = history_status(db)
    assert status["account_first_day"] == "unknown"
    assert status["plans"][0]["scheduling_complete"]
    assert status["windows"]["pending"] == len(jobs)


def test_five_day_outage_recovers_during_daytime(db):
    bind_account(db, ACCOUNT)
    settings = Settings(backfill_days=2, timezone="UTC")
    schedule_history(db, settings, NOW)
    for _ in range(3):
        schedule_history(db, settings, NOW + timedelta(days=5))
    dates = {job.payload["key"] for job in db.scalars(select(Job))}
    assert dates == {
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-12",
        "2026-09-13",
        "2026-09-14",
    }


def test_live_sync_and_diary_work_precede_older_history(db):
    bind_account(db, ACCOUNT)
    schedule_history(db, Settings(backfill_days=2), NOW - timedelta(hours=1))
    live = enqueue(
        db, "garmin_endpoint", {"endpoint": "heart_rate", "key": "2026-09-10"}, "live", NOW
    )
    assert claim(db, now=NOW, kinds=["garmin_endpoint"]).id == live


def test_history_does_not_block_context_questions(db):
    bind_account(db, ACCOUNT)
    schedule_history(db, Settings(backfill_days=2), NOW)
    proactive = enqueue(db, "agent_proactive", {}, "proactive", NOW)
    assert claim(db, now=NOW, kinds=["agent_proactive"]).id == proactive


def test_completed_empty_window_is_not_invented_history_start(db):
    bind_account(db, ACCOUNT)
    schedule_history(db, Settings(backfill_days=1), NOW)
    job = db.scalar(select(Job))
    complete_window(db, job.payload, {"status": "empty"}, NOW)
    db.flush()
    assert history_status(db)["windows"]["empty"] == 1
    assert history_status(db)["account_first_day"] == "unknown"


def test_no_history_until_owner_binding_and_explicit_disable(db):
    schedule_history(db, Settings(backfill_days=365), NOW)
    assert db.scalar(select(Job)) is None
    bind_account(db, ACCOUNT)
    schedule_history(db, Settings(backfill_days=0), NOW)
    assert db.scalar(select(Job)) is None


def test_terminal_failure_is_visible_without_discarding_cursor(db):
    bind_account(db, ACCOUNT)
    schedule_history(db, Settings(backfill_days=1), NOW)
    job = db.scalar(select(Job))
    job.status = "failed"
    db.flush()
    assert history_status(db)["windows"]["failed"] == 1
    assert db.scalar(select(AppState).where(AppState.key.startswith("syncplan:"))) is not None


def test_failed_old_history_does_not_suppress_current_context(db):
    from garmin_ai.jobs import failed_context_sync

    bind_account(db, ACCOUNT)
    schedule_history(db, Settings(backfill_days=1), NOW)
    job = db.scalar(select(Job).where(Job.payload["endpoint"].as_string() == "heart_rate"))
    job.status = "failed"
    job.completed_at = NOW
    db.flush()
    assert failed_context_sync(db, NOW) == []
    job.payload = {**job.payload, "backfill": False}
    db.flush()
    assert failed_context_sync(db, NOW)


def test_larger_horizon_does_not_spend_budget_on_existing_windows(db):
    bind_account(db, ACCOUNT)
    settings = Settings(backfill_days=30, timezone="UTC")
    for _ in range(15):
        schedule_history(db, settings, NOW)
    schedule_history(db, Settings(backfill_days=31, timezone="UTC"), NOW)
    assert db.scalar(select(func.count()).select_from(Job)) == 31 * DAY_ENDPOINTS


def test_new_endpoint_gets_old_history_generation(db, monkeypatch):
    from dataclasses import replace

    bind_account(db, ACCOUNT)
    settings = Settings(backfill_days=2, timezone="UTC")
    schedule_history(db, settings, NOW)
    candidate = replace(
        next(endpoint for endpoint in ENDPOINTS if endpoint.scope == "day"),
        name="synthetic_new_channel",
    )
    monkeypatch.setattr("garmin_ai.backfill.ENDPOINTS", [*ENDPOINTS, candidate])
    schedule_history(db, settings, NOW)
    assert db.scalar(select(func.count()).select_from(Job)) == 2 * (DAY_ENDPOINTS + 1)


def test_stale_window_finishes_with_current_provenance(db):
    from uuid import uuid4

    bind_account(db, ACCOUNT)
    schedule_history(db, Settings(backfill_days=1), NOW)
    job = db.scalar(select(Job))
    ref = str(uuid4())
    db.add(
        AppState(
            key=f"ingest:garmin_connect:{job.payload['endpoint']}:{job.payload['key']}",
            value={"source_ref": ref, "status": "normalized"},
        )
    )
    db.flush()
    complete_window(db, job.payload, {"status": "stale"}, NOW)
    db.flush()
    state = db.get(AppState, job.payload["sync_window"])
    assert state.value["status"] == "superseded"
    assert state.value["source_ref"] == ref
    assert state.value["completed_at"] == NOW.isoformat()
    assert history_status(db)["earliest_nonempty_window_date"] == job.payload["key"]
    assert history_status(db)["account_first_day"] == "unknown"
