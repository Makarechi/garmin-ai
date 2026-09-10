from datetime import UTC, datetime, timedelta

from garmin_ai.analytics import running_efficiency
from garmin_ai.models import Activity

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def add(db, identity, kind, offset, distance):
    db.add(
        Activity(
            id=identity,
            kind=kind,
            start=NOW + timedelta(hours=offset),
            end=NOW + timedelta(hours=offset + 1),
            timezone="UTC",
            duration_seconds=3600,
            moving_seconds=3600,
            distance_m=distance,
            avg_hr=150,
            details={},
        )
    )
    db.flush()


def test_treadmill_trail_and_running_are_not_globally_ranked(db):
    add(db, "road", "running", 0, 8000)
    add(db, "trail", "trail_running", 2, 12000)
    add(db, "treadmill", "treadmill_running", 4, 15000)
    result = running_efficiency(db, NOW, NOW + timedelta(days=1))
    assert [row["activity_id"] for row in result["rows"]] == ["road", "trail", "treadmill"]
    assert result["mixed_activity_types"]
    assert len(result["comparison_groups"]) == 3
    assert all(
        group["n"] == 1 and group["status"] == "descriptive_only"
        for group in result["comparison_groups"]
    )
    assert all(not row["physiologically_comparable"] for row in result["rows"])


def test_same_activity_type_alone_does_not_establish_comparability(db):
    add(db, "earlier", "running", 0, 8000)
    add(db, "later", "running", 2, 12000)
    result = running_efficiency(db, NOW, NOW + timedelta(days=1))
    assert not result["mixed_activity_types"]
    assert result["comparison_groups"][0]["activity_ids"] == ["earlier", "later"]
    assert result["rows"][0]["ascent_m_per_km"] is None
    for row in result["rows"]:
        assert "subjective_exertion" in row["comparison_missing"]
        assert "sensor_provenance" in row["comparison_missing"]
        assert not row["physiologically_comparable"]


def test_empty_analysis_has_no_invented_comparison(db):
    result = running_efficiency(db, NOW, NOW + timedelta(days=1))
    assert result["n"] == 0 and result["comparison_groups"] == []
