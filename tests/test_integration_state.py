from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from garminconnect import GarminConnectTooManyRequestsError
from sqlalchemy import select

from garmin_ai.accounts import ensure_account, profile_fingerprint
from garmin_ai.config import Settings
from garmin_ai.garmin import AuthenticationRequired, GarminReader
from garmin_ai.integration import (
    IntegrationBlocked,
    guarded,
    paused,
    record,
    resume_after_login,
    retry_after,
)
from garmin_ai.jobs import claim, enqueue
from garmin_ai.models import AppState, Job

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


@pytest.mark.parametrize("error", [AuthenticationRequired, GarminConnectTooManyRequestsError])
def test_one_failure_pauses_one_hundred_jobs_across_fresh_sessions(db, db_engine, error):
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        raise error("synthetic secret must never be persisted")

    with pytest.raises(error):
        guarded(db_engine, operation, now=NOW)
    for _ in range(100):
        with pytest.raises(IntegrationBlocked):
            guarded(db_engine, operation, now=NOW + timedelta(seconds=1))
    assert calls == 1
    row = db.get(AppState, "integration:garmin")
    assert "synthetic secret" not in str(row.value)
    assert row.value["failure_count"] == 1


def test_rate_limit_resumes_after_persisted_deadline(db, db_engine):
    error = GarminConnectTooManyRequestsError("synthetic")
    error.response = SimpleNamespace(headers={"Retry-After": "120"})

    def operation():
        raise error

    with pytest.raises(GarminConnectTooManyRequestsError):
        guarded(db_engine, operation, now=NOW)
    assert guarded(db_engine, lambda: "ok", now=NOW + timedelta(seconds=121)) == "ok"
    assert db.get(AppState, "integration:garmin").value["status"] == "active"


def test_paused_queue_keeps_attempts_and_other_work_available(db):
    record(db, "reauth_required", NOW, failure=True)
    garmin = enqueue(
        db, "garmin_endpoint", {"endpoint": "heart_rate", "key": "2026-09-10"}, "garmin", NOW
    )
    other = enqueue(db, "telegram_poll", {}, "poll", NOW)
    assert claim(db, now=NOW).id == other
    assert claim(db, now=NOW, kinds=["garmin_endpoint"]) is None
    assert db.get(Job, garmin).attempts == 0


def test_local_verified_login_resumes_existing_queue(db, db_engine):
    record(db, "reauth_required", NOW, failure=True)
    db.commit()
    settings = Settings(database_url=db_engine.url.render_as_string(hide_password=False))
    resume_after_login(settings)
    db.expire_all()
    assert not paused(db, datetime.now(UTC))


def test_auth_failure_metadata_does_not_prevent_first_owner_binding(db, db_engine):
    record(db, "reauth_required", NOW, failure=True)
    db.commit()
    identity = profile_fingerprint({"profileId": 123})
    assert ensure_account(db_engine, identity)["fingerprint"] == identity


def test_sdk_429_is_not_retried_within_same_job():
    calls = 0

    def fetch(*args):
        nonlocal calls
        calls += 1
        raise GarminConnectTooManyRequestsError("synthetic")

    reader = GarminReader(SimpleNamespace(get_stats=fetch), sleep=lambda _: None)
    with pytest.raises(GarminConnectTooManyRequestsError):
        reader.call("get_stats", "2026-09-10")
    assert calls == 1


def test_retry_after_date_and_unusable_values():
    error = SimpleNamespace(
        response=SimpleNamespace(headers={"Retry-After": "Thu, 10 Sep 2026 12:02:00 GMT"})
    )
    assert retry_after(error, NOW) == 120
    error.response.headers["Retry-After"] = "private-unparseable-url"
    assert retry_after(error, NOW) is None


def test_scheduler_does_not_expand_paused_queue(db):
    from garmin_ai.sync import schedule_sync

    record(db, "reauth_required", NOW, failure=True)
    schedule_sync(db, Settings(), NOW)
    assert db.scalar(select(Job)) is None


def test_pinned_sdk_refresh_persists_configured_token_store(tmp_path, monkeypatch):
    import json

    from garminconnect.client import Client

    client = Client()
    client.di_token = "synthetic-old-token"
    client._tokenstore_path = str(tmp_path)
    monkeypatch.setattr(
        client,
        "_refresh_di_token",
        lambda: setattr(client, "di_token", "synthetic-refreshed-token"),
    )
    client._refresh_session()
    assert (
        json.loads((tmp_path / "garmin_tokens.json").read_text())["di_token"]
        == "synthetic-refreshed-token"
    )
