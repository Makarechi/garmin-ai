from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from garmin_ai.metrics import convert
from garmin_ai.models import HealthDay, Measurement
from garmin_ai.normalize import normalize
from garmin_ai.queries import metric_series

START = datetime(2026, 9, 7, 12, tzinfo=UTC)


def sample(
    db,
    seconds,
    value,
    metric="heart_rate_bpm",
    unit="bpm",
    source="garmin_connect",
    quality="observed",
):
    db.add(
        Measurement(
            ts=START + timedelta(seconds=seconds),
            metric=metric,
            value=value,
            unit=unit,
            source=source,
            quality=quality,
            local_date=START.date(),
            source_ref=uuid4(),
        )
    )
    db.flush()


def test_increment_steps_are_summed(db):
    sample(db, 0, 10, "steps_bucket", "steps")
    sample(db, 120, 20, "steps_bucket", "steps")
    result = metric_series(db, "steps_bucket", START, START + timedelta(minutes=5))
    row = result["rows"][0]
    assert row["value"] == row["sum"] == 30
    assert row["mean"] is None
    assert row["covered_seconds"] is None
    assert result["contract"]["kind"] == "increment"


def test_daily_hydration_is_not_midnight_consumption(db):
    normalize(db, "hydration", "2026-09-07", {"valueInML": 1500}, uuid4(), "UTC")
    assert db.get(HealthDay, START.date()).hydration_ml == 1500
    assert db.scalar(select(Measurement)) is None
    with pytest.raises(ValueError, match="Unknown measurement"):
        metric_series(db, "hydration_ml", START, START + timedelta(hours=1))


def test_time_weighted_mean_uses_duration_and_clips_query(db):
    sample(db, 0, 60)
    sample(db, 60, 120)
    sample(db, 300, 90)
    row = metric_series(db, "heart_rate_bpm", START, START + timedelta(minutes=5))["rows"][0]
    assert row["mean"] == 108
    assert row["covered_seconds"] == 300
    assert row["coverage_ratio"] == 1
    assert len(row["source_refs"]) == 3
    partial = metric_series(
        db, "heart_rate_bpm", START + timedelta(seconds=60), START + timedelta(minutes=5)
    )["rows"][0]
    assert partial["mean"] == 120
    assert partial["covered_seconds"] == 240


def test_sparse_gauge_has_no_mean(db):
    sample(db, 0, 60)
    sample(db, 60, 100)
    sample(db, 600, 100)
    row = metric_series(db, "heart_rate_bpm", START, START + timedelta(minutes=5))["rows"][0]
    assert row["mean"] is None
    assert row["coverage_ratio"] == 0.2


def test_sentinels_unknown_units_and_quality_do_not_reach_analytics(db):
    sample(db, 0, -1)
    sample(db, 60, 70, unit="unknown")
    sample(db, 120, 80, quality="suspect")
    sample(db, 180, float("nan"))
    assert metric_series(db, "heart_rate_bpm", START, START + timedelta(minutes=5))["rows"] == []


def test_sources_are_not_added_or_bridged(db):
    sample(db, 0, 10, "steps_bucket", "steps", "garmin_connect")
    sample(db, 0, 10, "steps_bucket", "steps", "manual")
    rows = metric_series(db, "steps_bucket", START, START + timedelta(minutes=5))["rows"]
    assert len(rows) == 2
    assert [row["sum"] for row in rows] == [10, 10]


@pytest.mark.parametrize(
    "value,source,target,expected",
    [
        (1000, "ms", "s", 1),
        (120, "minutes", "hours", 2),
        (1500, "m", "km", 1.5),
        (10, "m/s", "km/h", 36),
        (4, "m/s", "s/km", 250),
    ],
)
def test_units(value, source, target, expected):
    assert convert(value, source, target) == expected


def test_unknown_unit_conversion_is_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        convert(10, "widgets", "ml")


def test_interval_crossing_bucket_boundary_is_split(db):
    sample(db, 240, 60)
    sample(db, 360, 60)
    rows = metric_series(db, "heart_rate_bpm", START, START + timedelta(minutes=10))["rows"]
    assert [row["covered_seconds"] for row in rows] == [60, 60]
    assert all(row["mean"] is None for row in rows)
