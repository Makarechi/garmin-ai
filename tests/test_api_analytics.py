from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from garmin_ai.analytics import block_mean_difference, compare_periods, describe, running_efficiency
from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.models import Activity, HealthDay
from garmin_ai.queries import timeline


def test_http_auth_idempotency_validation_and_revision(db, db_engine):
    settings = Settings(api_key=SecretStr("synthetic-test-api-key-with-32-characters"))
    client = TestClient(create_app(settings, db_engine))
    headers = {"Authorization": "Bearer " + settings.api_key.get_secret_value()}
    assert client.get("/health/ready").status_code == 200
    assert client.get("/tools").status_code == 401
    assert client.get("/tools", headers=headers).status_code == 200
    body = {
        "start": "2026-09-07T11:00:00+02:00",
        "payload": {"type": "caffeine", "beverage": "espresso"},
    }
    first = client.post("/events", headers={**headers, "Idempotency-Key": "http:1"}, json=body)
    assert first.status_code == 200
    second = client.post("/events", headers={**headers, "Idempotency-Key": "http:1"}, json=body)
    assert second.json()["id"] == first.json()["id"]
    event_id = first.json()["id"]
    assert (
        client.put(
            "/events/" + event_id, headers=headers, json={"revision": 3, "event": body}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/events", headers=headers, json={**body, "start": "2026-09-07T11:00:00"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/tools/health_snapshot", headers=headers, json={"arguments": {"day": "2026-09-07"}}
        ).json()["available"]
        is False
    )
    assert (
        client.post(
            "/tools/metric_series",
            headers=headers,
            json={
                "arguments": {
                    "metric": "heart_rate_bpm",
                    "start": "2026-09-07",
                    "end": "2026-09-08",
                }
            },
        ).status_code
        == 422
    )
    assert (
        client.delete("/events/" + event_id, headers=headers, params={"revision": 1}).status_code
        == 200
    )
    assert client.get("/events/" + event_id, headers=headers).status_code == 404


def test_analytics_reports_missing_and_computes_real_differences(db):
    start = date(2026, 7, 1)
    for i in range(40):
        db.add(
            HealthDay(
                day=start + timedelta(days=i), sleep_score=60 + i % 5 if i < 20 else 80 + i % 5
            )
        )
    db.flush()
    result = compare_periods(
        db,
        "sleep_score",
        start,
        start + timedelta(days=19),
        start + timedelta(days=20),
        start + timedelta(days=39),
    )
    assert result["a"]["n"] == 20 and result["b"]["n"] == 20
    assert result["difference"] == -20
    assert result["ci95"][1] < 0
    with pytest.raises(ValueError):
        compare_periods(
            db, "sleep_score", start, start + timedelta(days=10), start, start + timedelta(days=20)
        )
    assert describe([None, float("nan")])["mean"] is None
    assert block_mean_difference([1], [2])["ci95"] is None


def test_timeline_unknown_gaps_and_running_units(db):
    start = datetime(2026, 9, 7, 10, tzinfo=UTC)
    end = start + timedelta(hours=3)
    empty = timeline(db, start, end)
    assert empty["segments"][0]["status"] == "unknown"
    db.add(
        Activity(
            id="test-run",
            kind="running",
            start=start + timedelta(hours=1),
            end=start + timedelta(hours=2),
            timezone="Europe/Bratislava",
            duration_seconds=3600,
            distance_m=10000,
            avg_hr=150,
        )
    )
    db.flush()
    result = timeline(db, start, end)
    assert [s["status"] for s in result["segments"]] == ["unknown", "known", "unknown"]
    efficiency = running_efficiency(db, start, end)
    assert efficiency["n"] == 1
    assert efficiency["rows"][0]["meters_per_heartbeat"] == pytest.approx(10000 / 9000)
    assert efficiency["rows"][0]["sleep_score"] is None


def test_single_observation_has_no_standardized_effect(db):
    for i, value in enumerate([50, 60, 80]):
        db.add(HealthDay(day=date(2026, 9, 1) + timedelta(days=i), sleep_score=value))
    db.flush()
    result = compare_periods(
        db, "sleep_score", date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)
    )
    assert result["standardized_difference"] is None


def test_migraine_controls_outside_request_and_deterministic_ties(db):
    from garmin_ai.analytics import migraine_comparison
    from garmin_ai.events import EventInput, create_event

    day = date(2026, 9, 7)
    for offset in [7, 0, -7]:
        db.add(HealthDay(day=day + timedelta(days=offset), sleep_score=60 + offset))
    create_event(
        db, EventInput(start="2026-09-07T12:00:00Z", payload={"type": "migraine"}), actor="test"
    )
    db.flush()
    result = migraine_comparison(db, "sleep_score", day, day)
    assert result["matched_pairs"] == 1
    assert result["pairs"][0]["control_day"] == "2026-08-31"
    with pytest.raises(ValueError):
        migraine_comparison(db, "sleep_score", day, day, "Invalid/Zone")


def test_empty_idempotency_and_unknown_metric_are_invalid(db_engine):
    settings = Settings(api_key=SecretStr("x" * 32))
    client = TestClient(create_app(settings, db_engine))
    headers = {"Authorization": "Bearer " + "x" * 32}
    assert (
        client.post(
            "/events",
            headers={**headers, "Idempotency-Key": ""},
            json={"start": "2026-09-07T12:00:00Z", "payload": {"type": "migraine"}},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/tools/metric_series",
            headers=headers,
            json={
                "arguments": {
                    "metric": "typo",
                    "start": "2026-09-07T12:00:00Z",
                    "end": "2026-09-08T12:00:00Z",
                }
            },
        ).status_code
        == 422
    )


def test_history_refresh_does_not_mask_current_day(db):
    from garmin_ai.models import AppState
    from garmin_ai.queries import data_freshness

    now = datetime.now(UTC)
    db.add(
        AppState(
            key="freshness:sleep:old",
            value={"success_at": now.isoformat(), "source_key": "2020-01-01"},
        )
    )
    db.flush()
    result = data_freshness(db)
    assert "sleep" not in result["endpoints"] and "sleep" in result["historical"]


def test_api_passes_configured_timezone_to_tools(db_engine, monkeypatch):
    import garmin_ai.analytics

    seen = []
    monkeypatch.setattr(
        garmin_ai.analytics,
        "migraine_comparison",
        lambda session, metric, start, end, timezone: seen.append(timezone) or {},
    )
    settings = Settings(timezone="America/New_York", api_key=SecretStr("x" * 32))
    client = TestClient(create_app(settings, db_engine))
    result = client.post(
        "/tools/analysis_migraine_windows",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={"arguments": {"metric": "sleep_score", "start": "2026-09-01", "end": "2026-09-07"}},
    )
    assert result.status_code == 200 and seen == ["America/New_York"]
