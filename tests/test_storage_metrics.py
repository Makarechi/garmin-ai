from datetime import UTC, datetime, timedelta

import pytest

from garmin_ai.models import AppState
from garmin_ai.observability import backup_capacity, prometheus

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def report():
    return {
        "at": NOW.isoformat(),
        "status": "insufficient",
        "volumes": [
            {
                "role": "shared",
                "free_bytes": 100,
                "required_bytes": 200,
                "path": "synthetic private path",
            }
        ],
        "secret": "synthetic private secret",
    }


def test_capacity_metrics_report_values_and_age_without_private_fields(db):
    db.add(AppState(key="storage:backup-capacity", value=report()))
    db.flush()
    result = backup_capacity(db, NOW + timedelta(seconds=30))
    assert result["age_seconds"] == 30 and not result["sufficient"]
    text = prometheus(db)
    assert 'garmin_ai_backup_capacity_free_bytes{role="shared"} 100' in text
    assert 'garmin_ai_backup_capacity_required_bytes{role="shared"} 200' in text
    assert "private" not in text and "secret" not in text


@pytest.mark.parametrize(
    "changes",
    [
        {"at": "bad"},
        {"at": "2026-09-10T00:00:00"},
        {"at": "9999-01-01T00:00:00Z"},
        {"status": "private"},
        {"volumes": []},
        {"volumes": [None]},
        {"volumes": [{"role": "private", "free_bytes": 1, "required_bytes": 2}]},
        {"volumes": [{"role": "shared", "free_bytes": True, "required_bytes": 2}]},
        {"volumes": [{"role": "shared", "free_bytes": 2, "required_bytes": "secret"}]},
        {"volumes": [{"role": "shared", "free_bytes": 100, "required_bytes": 1}]},
    ],
)
def test_invalid_storage_reports_are_unknown_not_prometheus_labels(db, changes):
    db.add(AppState(key="storage:backup-capacity", value={**report(), **changes}))
    db.flush()
    assert not backup_capacity(db, NOW)["available"]
    assert "garmin_ai_backup_capacity_available 0" in prometheus(db)
    assert "secret" not in prometheus(db) and "private" not in prometheus(db)


def test_missing_capacity_report_does_not_claim_sufficient_disk(db):
    result = backup_capacity(db, NOW)
    assert not result["available"] and result["sufficient"] is None
