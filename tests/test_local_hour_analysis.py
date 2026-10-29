"""Local-hour grouping respects elapsed time, coverage, and timezone changes."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from garmin_ai.generic_analytics import AnalysisSpec, execute_analysis, spec_hash
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import Measurement
from garmin_ai.tools import call_tool


def plan(start, end, timezone="Europe/Bratislava", **changes):
    values = {
        "operation": "threshold_by_local_hour",
        "metric_key": "system.stress_score",
        "start": start,
        "end": end,
        "knowledge_cutoff": end + timedelta(days=1),
        "threshold": 51,
        "threshold_operator": "gte",
        "timezone": timezone,
    }
    return AnalysisSpec(**(values | changes))


def add(db, points):
    version = ensure_system_metric_definitions(db)["stress_score"]
    for at, value in points:
        db.add(
            Measurement(
                ts=at,
                metric="stress_score",
                source="synthetic",
                local_date=at.date(),
                value=value,
                unit="score",
                metric_definition_version_id=version.id,
                quality="observed",
                details={},
                ingested_at=at,
            )
        )
    db.flush()


def test_repeated_daylight_saving_hour_uses_both_actual_hours(db):
    zone = ZoneInfo("Europe/Bratislava")
    start = datetime(2026, 10, 25, tzinfo=zone)
    end = datetime(2026, 10, 26, tzinfo=zone)
    pivot = datetime(2026, 10, 25, 1, tzinfo=UTC)
    add(
        db,
        [
            (pivot - timedelta(minutes=2), 75),
            (pivot, 80),
            (pivot + timedelta(minutes=2), 20),
            (pivot + timedelta(minutes=5), 20),
        ],
    )
    result = call_tool(db, "generic_analysis", {"spec": plan(start, end).model_dump(mode="json")})
    hour = result["rows"][2]
    assert hour["scheduled_seconds"] == 7200
    assert hour["covered_seconds"] == 420
    assert hour["matching_seconds"] == 240
    assert hour["matching_ratio"] == pytest.approx(4 / 7)
    assert hour["coverage_ratio"] == pytest.approx(420 / 7200)
    assert hour["observed_days"] == 1
    assert sum(row["scheduled_seconds"] for row in result["rows"]) == 25 * 3600


def test_skipped_spring_hour_is_unknown_not_zero(db):
    zone = ZoneInfo("Europe/Bratislava")
    start = datetime(2026, 3, 29, tzinfo=zone)
    end = datetime(2026, 3, 30, tzinfo=zone)
    pivot = datetime(2026, 3, 29, 1, tzinfo=UTC)
    add(db, [(pivot - timedelta(minutes=2), 80), (pivot, 80), (pivot + timedelta(minutes=2), 20)])
    rows = execute_analysis(db, plan(start, end))["rows"]
    assert rows[1]["covered_seconds"] == 120
    assert rows[3]["covered_seconds"] == 120
    assert rows[2]["scheduled_seconds"] == 0
    assert rows[2]["coverage_ratio"] is None
    assert rows[2]["matching_seconds"] is None
    assert sum(row["scheduled_seconds"] for row in rows) == 23 * 3600


def test_half_hour_timezone_transition_splits_real_elapsed_time(db):
    start = datetime(2026, 4, 4, 14, 30, tzinfo=UTC)
    end = datetime(2026, 4, 4, 16, tzinfo=UTC)
    ensure_system_metric_definitions(db)
    rows = execute_analysis(db, plan(start, end, "Australia/Lord_Howe"))["rows"]
    assert rows[1]["scheduled_seconds"] == 3600
    assert rows[2]["scheduled_seconds"] == 1800
    assert rows[1]["matching_ratio"] is None
    assert sum(row["scheduled_seconds"] for row in rows) == 5400


@pytest.mark.parametrize("timezone", [None, "Not/A_Real_Zone", "../invalid"])
def test_local_hour_plan_requires_a_valid_timezone(timezone):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        plan(start, start + timedelta(days=1), timezone)


def test_other_operations_reject_grouping_timezone_and_preserve_old_hash():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    segments = plan(
        start, start + timedelta(hours=1), operation="threshold_segments", timezone=None
    )
    import json
    from hashlib import sha256

    previous = segments.model_dump(mode="json", exclude={"timezone"})
    assert (
        spec_hash(segments)
        == sha256(json.dumps(previous, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    with pytest.raises(ValidationError):
        plan(start, start + timedelta(hours=1), operation="threshold_segments")
