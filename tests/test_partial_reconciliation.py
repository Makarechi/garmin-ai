from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import ingest
from garmin_ai.models import AppState, Insight, Measurement
from garmin_ai.reconciliation import Replacement

START = datetime(2026, 9, 10, tzinfo=UTC)


def points(count, value=70):
    return {
        "heartRateValues": [
            [int((START + timedelta(minutes=minute)).timestamp() * 1000), value]
            for minute in range(count)
        ]
    }


def test_ten_point_partial_response_does_not_erase_seven_hundred_observations(db, tmp_path):
    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(700),
        "UTC",
        fetched_at=START + timedelta(hours=12),
    )
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(10, 80),
        "UTC",
        fetched_at=START + timedelta(hours=13),
    )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 700
    assert db.scalar(select(Measurement.value).where(Measurement.ts == START)) == 80
    assert (
        db.scalar(select(Measurement.value).where(Measurement.ts == START + timedelta(minutes=699)))
        == 70
    )
    assert (
        db.get(AppState, "ingest:garmin_connect:heart_rate:2026-09-10").value["completeness"]
        == "unverified"
    )


def test_attested_replacement_only_removes_covered_interval_and_invalidates_insights(db, tmp_path):
    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(20),
        "UTC",
        fetched_at=START + timedelta(hours=1),
    )
    db.add(
        Insight(
            category="synthetic",
            statement="synthetic",
            evidence={},
            sample_size=1,
            status="accepted",
            dedup_key="old",
        )
    )
    replacement = Replacement(
        START,
        START + timedelta(minutes=10),
        ("heart_rate_bpm",),
        "synthetic-authoritative-adapter:v1",
    )
    for _ in range(2):
        ingest(
            db,
            archive,
            "heart_rate",
            "2026-09-10",
            points(1, 90),
            "UTC",
            fetched_at=START + timedelta(hours=2),
            replacement=replacement,
        )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 11
    assert db.scalar(select(Measurement.value).where(Measurement.ts == START)) == 90
    assert (
        db.scalar(select(Measurement.value).where(Measurement.ts == START + timedelta(minutes=10)))
        == 70
    )
    assert db.scalar(select(Insight)).status == "superseded"
    state = db.get(AppState, "ingest:garmin_connect:heart_rate:2026-09-10", populate_existing=True)
    assert state.value["replacement"] == replacement.serialize()
    stale = ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(20),
        "UTC",
        fetched_at=START + timedelta(hours=1),
    )
    assert stale["status"] == "stale"
    assert db.scalar(select(func.count()).select_from(Measurement)) == 11


def test_attested_empty_snapshot_can_clear_only_its_channel(db, tmp_path):
    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(2),
        "UTC",
        fetched_at=START + timedelta(hours=1),
    )
    replacement = Replacement(
        START,
        START + timedelta(minutes=1),
        ("heart_rate_bpm",),
        "synthetic-authoritative-adapter:v1",
    )
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        {},
        "UTC",
        fetched_at=START + timedelta(hours=2),
        replacement=replacement,
    )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 1


@pytest.mark.parametrize(
    "metrics,evidence", [(("stress_score",), "synthetic"), (("heart_rate_bpm",), "")]
)
def test_replacement_requires_channel_contract_and_evidence(db, tmp_path, metrics, evidence):
    with pytest.raises(ValueError):
        ingest(
            db,
            LocalArchive(tmp_path),
            "heart_rate",
            "2026-09-10",
            {},
            "UTC",
            replacement=Replacement(START, START + timedelta(hours=1), metrics, evidence),
        )
