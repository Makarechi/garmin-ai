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
