from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.archive import LocalArchive
from garmin_ai.freshness import covered_seconds
from garmin_ai.ingest import ingest
from garmin_ai.models import AppState, HealthDay, Measurement
from garmin_ai.queries import data_freshness

NOW = datetime(2026, 9, 10, 18, tzinfo=UTC)


def point(db, at, metric="heart_rate_bpm", quality="observed"):
    db.add(
        Measurement(
            ts=at, local_date=at.date(), metric=metric, value=70, unit="bpm", quality=quality
        )
    )
    db.flush()


def test_fresh_fetch_does_not_hide_ten_hour_observation_lag(db):
    point(db, NOW - timedelta(hours=10))
    db.add(
        AppState(
            key="freshness:heart_rate:2026-09-10",
            value={
                "success_at": NOW.isoformat(),
                "status": "available",
                "source_key": "2026-09-10",
            },
        )
    )
    db.flush()
    result = data_freshness(db, NOW)
    assert result["endpoints"]["heart_rate"]["fetch_lag_seconds"] == 0
    channel = result["channels"]["heart_rate_bpm"]
    assert channel["observation_lag_seconds"] == 36000
    assert channel["quality_reason"] == "stale_observation"
    assert channel["usable_for_current_state"] is False
    assert channel["coverage_ratio"] == 0


def test_empty_fetch_preserves_history_without_promoting_coverage(db, tmp_path):
    archive = LocalArchive(tmp_path)
    payload = {"heartRateValues": [[int((NOW - timedelta(minutes=5)).timestamp() * 1000), 70]]}
    ingest(db, archive, "heart_rate", "2026-09-10", payload, "UTC", fetched_at=NOW)
    before = db.scalar(select(func.count()).select_from(Measurement))
    result = ingest(
        db, archive, "heart_rate", "2026-09-10", {}, "UTC", fetched_at=NOW + timedelta(seconds=1)
    )
    db.add(
        AppState(
            key="freshness:heart_rate:2026-09-10",
            value={
                "success_at": (NOW + timedelta(seconds=1)).isoformat(),
                "source_ref": result["source_ref"],
                "source_key": "2026-09-10",
                "status": result["status"],
            },
        )
    )
    db.flush()
    assert db.scalar(select(func.count()).select_from(Measurement)) == before == 1
    channel = data_freshness(db, NOW + timedelta(seconds=1))["channels"]["heart_rate_bpm"]
    assert channel["quality_reason"] == "source_empty"
    assert channel["coverage_ratio"] < 1
    assert channel["usable_for_current_state"] is False
    assert channel["source_ref"] != channel["fetch_source_ref"]


def test_nightly_summary_uses_calendar_recency_not_live_hr_threshold(db):
    db.add(HealthDay(day=NOW.date(), hrv_nightly_avg=50))
    point(db, NOW - timedelta(hours=6))
    result = data_freshness(db, NOW)["channels"]
    assert result["hrv_nightly_avg"]["quality_reason"] == "recent_daily_summary"
    assert result["hrv_nightly_avg"]["newest_observed_at"] is None
    assert result["hrv_nightly_avg"]["usable_as_daily_summary"] is True
    assert result["heart_rate_bpm"]["quality_reason"] == "stale_observation"


def test_coverage_never_bridges_long_gaps_or_fills_after_last_sample():
    points = [
        NOW - timedelta(hours=5),
        NOW - timedelta(hours=4),
        NOW - timedelta(minutes=4),
        NOW - timedelta(minutes=2),
    ]
    assert covered_seconds(points, NOW - timedelta(hours=6), NOW, 300) == 120
    assert covered_seconds(points * 2, NOW - timedelta(hours=6), NOW, 300) == 120


def test_dense_recent_observations_pass_gate_but_suspect_samples_do_not(db):
    for minutes in range(0, 31, 2):
        point(db, NOW - timedelta(minutes=minutes))
    result = data_freshness(db, NOW)["channels"]["heart_rate_bpm"]
    assert result["usable_for_current_state"] is True
    assert result["recent_coverage_ratio"] == 1
    point(db, NOW + timedelta(minutes=1), quality="suspect")
    assert (
        data_freshness(db, NOW + timedelta(hours=1))["channels"]["heart_rate_bpm"][
            "usable_for_current_state"
        ]
        is False
    )


@pytest.mark.parametrize("days", [3, 30])
def test_old_last_observation_remains_visible(db, days):
    point(db, NOW - timedelta(days=days))
    channel = data_freshness(db, NOW)["channels"]["heart_rate_bpm"]
    assert channel["observation_lag_seconds"] == days * 86400
    assert channel["quality_reason"] == "stale_observation"


def test_context_question_rejects_sparse_or_suspect_hr(db):
    from garmin_ai.proactive import context_physiology

    left = NOW - timedelta(minutes=22)
    for minutes in range(0, 22, 2):
        point(db, left + timedelta(minutes=minutes), metric="stress_score")
    # Set stress high enough, but cluster five HR points in one minute.
    from sqlalchemy import update

    db.execute(update(Measurement).where(Measurement.metric == "stress_score").values(value=90))
    for seconds in range(0, 50, 10):
        point(db, left + timedelta(seconds=seconds))
    assert context_physiology(db, "UTC", NOW, left, NOW, threshold=60) is None


def test_failed_first_fetch_has_no_invented_success_and_late_attempt_cannot_replace_newer(db):
    from garmin_ai.sync import record_endpoint_fetch

    record_endpoint_fetch(db, "heart_rate", "2026-09-10", NOW, {"status": "fetch_error"})
    first = data_freshness(db, NOW)
    assert first["endpoints"]["heart_rate"]["last_success_at"] is None
    assert first["channels"]["heart_rate_bpm"]["quality_reason"] == "fetch_error"
    record_endpoint_fetch(
        db, "heart_rate", "2026-09-10", NOW - timedelta(hours=1), {"status": "available"}
    )
    assert data_freshness(db, NOW)["endpoints"]["heart_rate"]["status"] == "fetch_error"
    record_endpoint_fetch(
        db, "heart_rate", "2026-09-10", NOW + timedelta(minutes=1), {"status": "available"}
    )
    record_endpoint_fetch(
        db, "heart_rate", "2026-09-10", NOW + timedelta(minutes=2), {"status": "fetch_error"}
    )
    final = data_freshness(db, NOW + timedelta(minutes=2))["endpoints"]["heart_rate"]
    assert final["last_success_at"] == (NOW + timedelta(minutes=1)).isoformat()
    assert final["fetch_lag_seconds"] == 0


@pytest.mark.parametrize("values", [[], [[1789063200000, -1]]])
def test_empty_envelope_cannot_borrow_dense_prior_samples(db, tmp_path, values):
    from garmin_ai.sync import record_endpoint_fetch

    for minutes in range(0, 31, 2):
        point(db, NOW - timedelta(minutes=minutes))
    result = ingest(
        db,
        LocalArchive(tmp_path),
        "heart_rate",
        "2026-09-10",
        {"heartRateValues": values},
        "UTC",
        fetched_at=NOW,
    )
    record_endpoint_fetch(db, "heart_rate", "2026-09-10", NOW, result)
    channel = data_freshness(db, NOW)["channels"]["heart_rate_bpm"]
    assert channel["recent_coverage_ratio"] == 1
    assert channel["quality_reason"] == "source_empty"
    assert channel["usable_for_current_state"] is False


def test_body_battery_follows_stress_fetch_not_daily_totals(db):
    from garmin_ai.sync import record_endpoint_fetch

    for minutes in range(0, 61, 5):
        point(db, NOW - timedelta(minutes=minutes), metric="body_battery")
    record_endpoint_fetch(db, "stress", "2026-09-10", NOW, {"status": "normalized"})
    record_endpoint_fetch(db, "body_battery", "2026-09-10", NOW, {"status": "fetch_error"})
    assert data_freshness(db, NOW)["channels"]["body_battery"]["usable_for_current_state"]
    record_endpoint_fetch(db, "stress", "2026-09-10", NOW, {"status": "fetch_error"})
    record_endpoint_fetch(db, "body_battery", "2026-09-10", NOW, {"status": "normalized"})
    assert not data_freshness(db, NOW)["channels"]["body_battery"]["usable_for_current_state"]


def test_parser_error_does_not_clear_failed_context_fence(db, tmp_path):
    from garmin_ai.jobs import failed_context_sync
    from garmin_ai.models import Job
    from garmin_ai.sync import record_endpoint_fetch

    db.add(
        Job(
            kind="garmin_endpoint",
            payload={"endpoint": "heart_rate", "key": "2026-09-10"},
            dedup_key="synthetic-failure",
            status="failed",
            run_at=NOW,
            completed_at=NOW,
        )
    )
    db.flush()
    record_endpoint_fetch(
        db, "heart_rate", "2026-09-10", NOW + timedelta(seconds=1), {"status": "error"}
    )
    assert failed_context_sync(db, NOW + timedelta(seconds=2))
    state = db.get(AppState, "freshness:heart_rate:2026-09-10", populate_existing=True)
    assert state.value["success_at"] is not None
    assert state.value["normalized_at"] is None
    for index, payload in enumerate(
        ({}, {"heartRateValues": []}, {"heartRateValues": [[int(NOW.timestamp() * 1000), -1]]}), 3
    ):
        at = NOW + timedelta(seconds=index)
        result = ingest(
            db, LocalArchive(tmp_path), "heart_rate", "2026-09-10", payload, "UTC", fetched_at=at
        )
        record_endpoint_fetch(db, "heart_rate", "2026-09-10", at, result)
        assert failed_context_sync(db, at)
    at = NOW + timedelta(seconds=6)
    result = ingest(
        db,
        LocalArchive(tmp_path),
        "heart_rate",
        "2026-09-10",
        {"heartRateValues": [[int(NOW.timestamp() * 1000), 70]]},
        "UTC",
        fetched_at=at,
    )
    record_endpoint_fetch(db, "heart_rate", "2026-09-10", at, result)
    assert failed_context_sync(db, at) == []


@pytest.mark.parametrize("age", [1, 3, 30])
def test_observed_interval_uses_only_current_day_points(db, age):
    point(db, NOW - timedelta(days=age))
    channel = data_freshness(db, NOW)["channels"]["heart_rate_bpm"]
    assert channel["observed_interval"] == {"start": None, "end": None}
    point(db, NOW - timedelta(minutes=5))
    channel = data_freshness(db, NOW)["channels"]["heart_rate_bpm"]
    assert channel["observed_interval"] == {
        "start": (NOW - timedelta(minutes=5)).isoformat(),
        "end": (NOW - timedelta(minutes=5)).isoformat(),
    }


@pytest.mark.parametrize(
    "kind,payload,endpoint,key",
    [
        ("garmin_activities", {"offset": 0, "since": "2026-09-01"}, "activities", "page:0"),
        ("garmin_fit", {"activity_id": "123"}, "activity_fit", "123"),
    ],
)
def test_activity_fetch_failures_are_persisted_before_raise(
    db, db_engine, tmp_path, kind, payload, endpoint, key
):
    from types import SimpleNamespace

    from garmin_ai.config import Settings
    from garmin_ai.sync import run_garmin_job

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic fetch failure")

    with pytest.raises(RuntimeError):
        run_garmin_job(
            db_engine, SimpleNamespace(call=fail), LocalArchive(tmp_path), Settings(), kind, payload
        )
    state = db.get(AppState, f"freshness:{endpoint}:{key}")
    assert state.value["status"] == "fetch_error"
    assert state.value["success_at"] is None


def test_invalid_activity_page_exposes_normalization_error(db, db_engine, tmp_path):
    from types import SimpleNamespace

    from garmin_ai.config import Settings
    from garmin_ai.sync import run_garmin_job

    with pytest.raises(ValueError):
        run_garmin_job(
            db_engine,
            SimpleNamespace(call=lambda *args: {}),
            LocalArchive(tmp_path),
            Settings(),
            "garmin_activities",
            {"offset": 0, "since": "2026-09-01"},
        )
    endpoint = data_freshness(db)["endpoints"]["activities"]
    assert endpoint["status"] == "error"
    assert endpoint["success_at"] is not None
    assert endpoint["normalized_at"] is None


def test_fit_parser_failure_records_successful_response_but_not_normalization(
    db, db_engine, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from garmin_ai.config import Settings
    from garmin_ai.sync import run_garmin_job

    monkeypatch.setattr("garmin_ai.sync.store_fit", lambda *args, **kwargs: {"status": "error"})
    with pytest.raises(ValueError, match="FIT parsing"):
        run_garmin_job(
            db_engine,
            SimpleNamespace(call=lambda *args, **kwargs: b"synthetic invalid fit"),
            LocalArchive(tmp_path),
            Settings(),
            "garmin_fit",
            {"activity_id": "123"},
        )
    endpoint = data_freshness(db)["endpoints"]["activity_fit"]
    assert endpoint["status"] == "error"
    assert endpoint["success_at"] is not None
    assert endpoint["normalized_at"] is None


def test_freshness_reduces_many_activity_rows_in_database(db):
    from garmin_ai.queries import latest_freshness_rows

    for index in range(300):
        db.add(
            AppState(
                key=f"freshness:activity:{index}",
                value={
                    "source_key": str(index),
                    "fetched_at": (NOW - timedelta(seconds=index)).isoformat(),
                    "status": "normalized",
                },
            )
        )
        db.add(
            AppState(
                key=f"freshness:heart_rate:2025-01-01:{index}",
                value={
                    "source_key": "2025-01-01",
                    "fetched_at": (NOW - timedelta(seconds=index)).isoformat(),
                    "status": "normalized",
                },
            )
        )
    db.flush()
    rows = latest_freshness_rows(db, NOW.date())
    assert len(rows) == 2
    assert {row.key for row in rows} == {
        "freshness:activity:0",
        "freshness:heart_rate:2025-01-01:0",
    }
    assert data_freshness(db, NOW)["endpoints"]["activity"]["source_key"] == "0"


@pytest.mark.parametrize("status", ["normalized", "empty", "error"])
def test_fit_outcomes_retain_payload_metadata(db, tmp_path, monkeypatch, status):
    from garmin_ai.fit import store_fit
    from garmin_ai.models import Activity
    from garmin_ai.sync import record_endpoint_fetch

    db.add(
        Activity(
            id="123", kind="running", start=NOW, end=NOW + timedelta(minutes=30), timezone="UTC"
        )
    )
    db.flush()
    if status == "normalized":
        monkeypatch.setattr("garmin_ai.fit.parse_fit", lambda _: [])
    result = store_fit(
        db,
        LocalArchive(tmp_path),
        "123",
        b"" if status == "empty" else b"synthetic",
        fetched_at=NOW,
    )
    assert result["status"] == status
    record_endpoint_fetch(db, "activity_fit", "123", NOW, result)
    value = data_freshness(db, NOW)["endpoints"]["activity_fit"]
    assert value["source_ref"] == result["source_ref"]
    assert value["source_revision"]
    assert value["parser_status"] == status
    assert "parser_version" in value


def test_channel_recency_index_is_installed(db):
    from sqlalchemy import inspect

    indexes = {index["name"]: index for index in inspect(db.get_bind()).get_indexes("measurements")}
    assert indexes["ix_measurements_metric_quality_ts"]["column_names"] == [
        "metric",
        "quality",
        "ts",
    ]
