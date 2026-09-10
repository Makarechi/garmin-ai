from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

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


def confirmed_headache_free_day(db, day):
    from garmin_ai.events import EventInput, create_event

    start = datetime.combine(day, datetime.min.time(), ZoneInfo("Europe/Bratislava"))
    create_event(
        db,
        EventInput(
            start=start,
            end=start + timedelta(days=1),
            payload={"type": "headache_observation", "headache": "no", "migraine": "no"},
        ),
        actor="test",
    )


def test_migraine_comparison_outside_request_and_deterministic_ties(db):
    from garmin_ai.analytics import migraine_comparison
    from garmin_ai.events import EventInput, create_event

    day = date(2026, 9, 7)
    for offset in [7, 0, -7]:
        db.add(HealthDay(day=day + timedelta(days=offset), sleep_score=60 + offset))
        if offset:
            confirmed_headache_free_day(db, day + timedelta(days=offset))
    create_event(
        db,
        EventInput(
            start="2026-09-07T12:00:00Z", end="2026-09-07T15:00:00Z", payload={"type": "migraine"}
        ),
        actor="test",
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


def test_control_assignment_maximizes_valid_pairs(db):
    from garmin_ai.analytics import migraine_comparison
    from garmin_ai.events import EventInput, create_event

    day = date(2026, 9, 7)
    for offset in (-56, 0, 7, 14):
        db.add(HealthDay(day=day + timedelta(days=offset), sleep_score=70))
        if offset in (-56, 7):
            confirmed_headache_free_day(db, day + timedelta(days=offset))
    for offset in (0, 14):
        create_event(
            db,
            EventInput(
                start=datetime.combine(day + timedelta(days=offset), datetime.min.time(), UTC),
                end=datetime.combine(day + timedelta(days=offset), datetime.min.time(), UTC)
                + timedelta(hours=3),
                payload={"type": "migraine"},
            ),
            actor="test",
        )
    db.flush()
    assert (
        migraine_comparison(db, "sleep_score", day, day + timedelta(days=14))["matched_pairs"] == 2
    )
    with pytest.raises(ValueError):
        migraine_comparison(db, "sleep_score", date.max, date.max)


def test_activity_samples_can_be_paginated(db):
    from garmin_ai.models import ActivityPart
    from garmin_ai.queries import activity_details

    now = datetime.now(UTC)
    db.add(Activity(id="page", kind="running", start=now, end=now, timezone="UTC"))
    db.flush()
    for i in range(3):
        db.add(
            ActivityPart(
                activity_id="page", kind="fit_record", sequence=i, payload={"synthetic": i}
            )
        )
    db.flush()
    first = activity_details(db, "page", True, limit=2)
    second = activity_details(db, "page", True, offset=first["next_offset"], limit=2)
    assert [r["sequence"] for r in first["parts"] + second["parts"]] == [0, 1, 2]
    assert second["next_offset"] is None


def test_extreme_calendar_dates_are_rejected_before_expansion(db):
    from garmin_ai.analytics import lagged_association
    from garmin_ai.queries import time_range

    with pytest.raises(ValueError):
        lagged_association(db, "sleep_score", "resting_hr", date.min, date.min, [-1])
    with pytest.raises(ValueError):
        time_range(datetime(9999, 12, 30, tzinfo=UTC), datetime(9999, 12, 31, tzinfo=UTC))


def test_running_context_does_not_infer_sleep_from_configured_day(db):
    db.info["timezone"] = "UTC"
    instant = datetime(2026, 9, 7, 23, tzinfo=UTC)
    db.add(
        Activity(
            id="travel",
            kind="running",
            start=instant,
            end=instant + timedelta(minutes=30),
            timezone="Asia/Tokyo",
            duration_seconds=1800,
            distance_m=5000,
            avg_hr=140,
        )
    )
    db.add(HealthDay(day=date(2026, 9, 7), sleep_score=55))
    db.add(HealthDay(day=date(2026, 9, 8), sleep_score=88))
    db.flush()
    result = running_efficiency(db, instant - timedelta(hours=1), instant + timedelta(hours=1))
    assert result["rows"][0]["sleep_score"] is None
    assert result["rows"][0]["context"]["sleep_score"]["quality"] == "unknown"


def test_delete_rejects_invalid_revision(db_engine):
    from uuid import uuid4

    settings = Settings(api_key="synthetic-test-api-key-with-32-characters")
    client = TestClient(create_app(settings, db_engine))
    response = client.delete(
        f"/events/{uuid4()}?revision=0",
        headers={"Authorization": "Bearer " + settings.api_key.get_secret_value()},
    )
    assert response.status_code == 422


def test_empty_activity_and_fit_sync_record_freshness(db, db_engine, tmp_path):
    from unittest.mock import patch

    from garmin_ai.archive import LocalArchive
    from garmin_ai.queries import data_freshness
    from garmin_ai.sync import run_garmin_job

    class Reader:
        def call(self, method, *args, **kwargs):
            return [] if method == "get_activities" else b""

    reader = Reader()
    archive = LocalArchive(tmp_path)
    run_garmin_job(
        db_engine,
        reader,
        archive,
        Settings(),
        "garmin_activities",
        {"offset": 0, "since": "2026-09-01"},
    )
    with patch("garmin_ai.sync.store_fit", return_value={"status": "empty"}):
        run_garmin_job(db_engine, reader, archive, Settings(), "garmin_fit", {"activity_id": "123"})
    result = data_freshness(db)
    assert result["available"]
    assert result["endpoints"]["activities"]["status"] == "empty"
    assert result["endpoints"]["activity_fit"]["status"] == "empty"


def test_timestamp_ranges_compare_instants():
    from garmin_ai.queries import time_range

    time_range(
        datetime.fromisoformat("2026-01-02T00:00+14:00"),
        datetime.fromisoformat("2026-01-01T23:00-12:00"),
    )


@pytest.mark.parametrize("kind", ["track_running", "indoor_running", "ultra_run"])
def test_running_subtypes_are_included(db, kind):
    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    db.add(
        Activity(
            id="subtype",
            kind=kind,
            start=instant,
            end=instant + timedelta(minutes=30),
            timezone="UTC",
            duration_seconds=1800,
            distance_m=5000,
            avg_hr=140,
        )
    )
    db.flush()
    assert (
        running_efficiency(db, instant - timedelta(hours=1), instant + timedelta(hours=1))["n"] == 1
    )


def test_migraine_matching_rejects_unbounded_episode_count(db):
    from garmin_ai.analytics import migraine_comparison
    from garmin_ai.events import EventInput, create_event

    start = date(2025, 1, 1)
    for i in range(201):
        day = start + timedelta(days=i)
        db.add(HealthDay(day=day, sleep_score=70))
        create_event(
            db,
            EventInput(
                start=datetime.combine(day, datetime.min.time(), UTC), payload={"type": "migraine"}
            ),
            actor="test",
        )
    db.flush()
    with pytest.raises(ValueError, match="200"):
        migraine_comparison(db, "sleep_score", start, start + timedelta(days=201))


def test_history_validates_kind_and_includes_overlap(db):
    from garmin_ai.events import EventInput, create_event
    from garmin_ai.queries import list_events

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    interval = create_event(
        db,
        EventInput(
            start=now - timedelta(days=1),
            end=now + timedelta(hours=2),
            payload={"type": "illness", "description": "synthetic"},
        ),
        actor="owner",
    )
    create_event(
        db,
        EventInput(
            start=now - timedelta(days=1), payload={"type": "note", "description": "old point"}
        ),
        actor="owner",
    )
    result = list_events(db, now, now + timedelta(hours=1))
    assert [row["id"] for row in result["rows"]] == [str(interval.id)]
    with pytest.raises(ValueError, match="Unknown event kind"):
        list_events(db, now, now + timedelta(hours=1), "migrane")


def test_insight_cursor_retrieves_history_with_timestamp_ties(db):
    from garmin_ai.models import Insight
    from garmin_ai.queries import insights_list

    instant = datetime(2026, 9, 7, 12, tzinfo=UTC)
    for i in range(105):
        db.add(
            Insight(
                category="synthetic",
                sample_size=0,
                statement="test",
                evidence={},
                dedup_key=f"paging-{i}",
                generated_at=instant,
            )
        )
    db.flush()
    first = insights_list(db, 100)
    second = insights_list(db, 100, first["next_cursor"])
    assert first["truncated"] and not second["truncated"] and second["next_cursor"] is None
    assert len({r["id"] for r in first["rows"] + second["rows"]}) == 105
    with pytest.raises(ValueError, match="cursor"):
        insights_list(db, 30, "not-a-cursor")
