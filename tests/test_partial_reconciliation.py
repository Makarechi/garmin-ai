from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import ingest
from garmin_ai.models import AppState, Insight, Measurement
from garmin_ai.reconciliation import Replacement

START = datetime(2026, 9, 10, tzinfo=UTC)


def test_summary_only_body_battery_endpoint_rejects_sample_replacement():
    with pytest.raises(ValueError, match="channels"):
        Replacement(START, START + timedelta(hours=1), ("body_battery",), "synthetic").validate(
            "body_battery"
        )


def test_hrv_authoritative_interval_removes_omitted_samples(db, tmp_path):
    archive = LocalArchive(tmp_path)
    payload = {
        "hrvReadings": [
            {
                "readingTimeGMT": int((START + timedelta(minutes=i)).timestamp() * 1000),
                "hrvValue": 40,
            }
            for i in range(2)
        ]
    }
    ingest(db, archive, "hrv", "2026-09-10", payload, "UTC", fetched_at=START)
    ingest(
        db,
        archive,
        "hrv",
        "2026-09-10",
        {},
        "UTC",
        fetched_at=START + timedelta(hours=1),
        replacement=Replacement(
            START, START + timedelta(minutes=1), ("hrv_rmssd_ms",), "synthetic"
        ),
    )
    rows = db.scalars(select(Measurement)).all()
    assert len(rows) == 1 and rows[0].ts == START + timedelta(minutes=1)


def test_alternate_source_does_not_overwrite_or_clear_garmin_samples(db, tmp_path):
    archive = LocalArchive(tmp_path)
    for source, value in [("garmin_connect", 70), ("synthetic_import", 90)]:
        ingest(
            db,
            archive,
            "heart_rate",
            "2026-09-10",
            points(2, value),
            "UTC",
            source=source,
            fetched_at=START,
        )
    assert db.scalar(select(func.count()).select_from(Measurement)) == 4
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        {},
        "UTC",
        source="synthetic_import",
        fetched_at=START + timedelta(hours=1),
        replacement=Replacement(
            START, START + timedelta(minutes=1), ("heart_rate_bpm",), "synthetic"
        ),
    )
    rows = db.scalars(select(Measurement)).all()
    assert len(rows) == 3
    assert len([r for r in rows if r.source == "garmin_connect" and r.value == 70]) == 2


def test_metric_order_and_duplicates_do_not_reapply_identical_contract(db, tmp_path):
    archive = LocalArchive(tmp_path)
    results = []
    for metrics in [
        ("stress_score", "body_battery"),
        ("body_battery", "stress_score", "stress_score"),
    ]:
        results.append(
            ingest(
                db,
                archive,
                "stress",
                "2026-09-10",
                {},
                "UTC",
                fetched_at=START,
                replacement=Replacement(START, START + timedelta(hours=1), metrics, "synthetic"),
            )
        )
    assert [r["status"] for r in results] == ["empty", "unchanged"]


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


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("live", [False, True])
def test_parser_replay_rebuilds_current_revision_atomically(db, tmp_path, monkeypatch, fail, live):
    from garmin_ai.config import Settings
    from garmin_ai.models import SourcePayload
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    ingest(db, archive, "heart_rate", "2026-09-10", points(3), "UTC", fetched_at=START)
    ingest(
        db,
        archive,
        "heart_rate",
        "2026-09-10",
        points(1, 80),
        "UTC",
        fetched_at=START + timedelta(hours=1),
    )
    current = db.scalar(
        select(SourcePayload).where(
            SourcePayload.payload_hash
            == db.get(AppState, "ingest:garmin_connect:heart_rate:2026-09-10").value["hash"]
        )
    )
    current.parser_version = PARSER_VERSION - 1
    db.add(
        Measurement(
            ts=START,
            metric="obsolete_parser_metric",
            local_date=START.date(),
            source="garmin_connect",
            source_ref=current.id,
            value=1,
            unit="synthetic",
            quality="valid",
        )
    )
    db.flush()
    if fail:

        def broken(*args):
            raise ValueError("synthetic parser failure")

        monkeypatch.setattr("garmin_ai.ingest.normalize", broken)
    result = (
        ingest(
            db,
            archive,
            "heart_rate",
            "2026-09-10",
            points(1, 80),
            "UTC",
            fetched_at=START + timedelta(hours=2),
        )
        if live
        else replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": str(current.id), "target_version": PARSER_VERSION},
        )
    )
    obsolete = db.scalar(select(Measurement).where(Measurement.metric == "obsolete_parser_metric"))
    assert (obsolete is not None) == fail
    assert result["status"] == ("error" if fail else "normalized")
    readings = db.scalars(
        select(Measurement).where(Measurement.metric == "heart_rate_bpm").order_by(Measurement.ts)
    ).all()
    assert [row.value for row in readings] == [80, 70, 70]


def test_replacement_order_uses_absolute_instants_during_dst_fold():
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Bratislava")
    first = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    second = datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0)
    with pytest.raises(ValueError, match="positive"):
        Replacement(first, second, ("heart_rate_bpm",), "synthetic").validate("heart_rate")
    valid = Replacement(second, first, ("heart_rate_bpm",), "synthetic")
    valid.validate("heart_rate")
    encoded = valid.serialize()
    assert datetime.fromisoformat(encoded["end"]) - datetime.fromisoformat(
        encoded["start"]
    ) == timedelta(minutes=30)
