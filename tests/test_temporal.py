from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from garmin_ai.analytics import running_efficiency
from garmin_ai.models import Activity, HealthDay, MetricObservation
from garmin_ai.normalize import normalize
from garmin_ai.temporal import explicit_time, feature_at


def instant(hour):
    return datetime(2026, 3, 29, hour, tzinfo=UTC)


def readiness(db, rows, ref=None):
    normalize(db, "readiness", "2026-03-29", rows, ref or uuid4(), "Europe/Budapest")
    db.flush()


def at(db, hour=10, **kwargs):
    return feature_at(
        db,
        "training_readiness_score",
        instant(hour),
        kwargs.pop("knowledge_cutoff", datetime.now(UTC)),
        **kwargs,
    )


def test_before_run_version_not_daily_latest(db):
    readiness(
        db,
        [
            {"timestamp": instant(8).isoformat(), "score": 70},
            {"timestamp": instant(12).isoformat(), "score": 35},
        ],
    )
    assert db.get(HealthDay, date(2026, 3, 29)).training_readiness_score == 35
    result = at(db)
    assert result["value"] == 70
    assert result["source_ref"] and result["observation_id"]
    assert result["event_cutoff"] == instant(10).isoformat()
    assert result["feature_version"] == "pre-event-v1"


@pytest.mark.parametrize("timestamp", [instant(12).isoformat(), "2026-03-29T08:00:00", None])
def test_post_event_and_ambiguous_time_are_unknown(db, timestamp):
    readiness(db, {"timestamp": timestamp, "score": 35})
    assert at(db)["value"] is None


def test_correction_has_distinct_knowledge_time(db):
    readiness(db, {"timestamp": instant(8).isoformat(), "score": 70})
    first = db.scalar(select(MetricObservation))
    first.ingested_at = instant(9)
    readiness(db, {"timestamp": instant(8).isoformat(), "score": 65})
    second = db.scalar(select(MetricObservation).where(MetricObservation.id != first.id))
    second.ingested_at = instant(12)
    db.flush()
    assert at(db, purpose="as_known", knowledge_cutoff=instant(10))["value"] == 70
    assert at(db, knowledge_cutoff=instant(13))["value"] == 65
    with pytest.raises(ValueError, match="cannot follow"):
        at(db, purpose="as_known", knowledge_cutoff=instant(13))


def test_replay_does_not_duplicate_or_backdate_knowledge(db):
    ref = uuid4()
    for _ in range(2):
        db.info["fetch_time"] = instant(9)
        readiness(db, {"timestamp": instant(8).isoformat(), "score": 70}, ref)
    assert db.scalar(select(func.count()).select_from(MetricObservation)) == 1
    assert at(db, purpose="as_known", knowledge_cutoff=instant(10))["value"] is None


def test_sleep_context_uses_completed_session_across_dst_and_travel(db):
    # Tokyo activity label and Budapest configuration must not select a daily row.
    end = instant(7)
    normalize(
        db,
        "sleep",
        "2026-03-30",
        {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": int((end - timedelta(hours=8)).timestamp() * 1000),
                "sleepEndTimestampGMT": int(end.timestamp() * 1000),
                "sleepScores": {"overall": {"value": 81}},
            }
        },
        uuid4(),
        "Europe/Budapest",
    )
    readiness(
        db,
        [
            {"timestamp": instant(8).isoformat(), "score": 70},
            {"timestamp": instant(12).isoformat(), "score": 35},
        ],
    )
    db.add(
        Activity(
            id="travel",
            kind="running",
            start=instant(10),
            end=instant(11),
            timezone="Asia/Tokyo",
            duration_seconds=3600,
            distance_m=10000,
            avg_hr=140,
        )
    )
    db.flush()
    row = running_efficiency(db, instant(9), instant(13))["rows"][0]
    assert row["readiness"] == 70
    assert row["sleep_score"] == 81
    assert row["context"]["sleep_score"]["source_calendar_date"] == "2026-03-30"
    assert feature_at(db, "sleep_score", end, datetime.now(UTC))["value"] is None
    assert (
        feature_at(db, "sleep_score", end + timedelta(hours=25), datetime.now(UTC))["value"] is None
    )


def test_offset_timestamp_is_absolute():
    assert explicit_time("2026-03-29T10:00:00+02:00") == instant(8)
    assert explicit_time("broken") is None


def test_unknown_purpose_rejected(db):
    with pytest.raises(ValueError, match="purpose"):
        at(db, purpose="guess")


@pytest.mark.parametrize("revision", ["bfccd06bf1c6", "4c9e28f110ab"])
def test_previous_exports_restore_with_empty_versions(db, db_engine, tmp_path, revision):
    import gzip
    import json

    from garmin_ai.models import Base
    from garmin_ai.operations import restore_database

    path = tmp_path / "old.gz"
    counts = {name: 0 for name in Base.metadata.tables if name != "metric_observations"}
    db.commit()
    with gzip.open(path, "wt") as output:
        output.write(json.dumps({"format": "garmin-ai-jsonl-v1", "revision": revision}) + "\n")
        output.write(json.dumps({"counts": counts}) + "\n")
    assert restore_database(db_engine, path)["metric_observations"] == 0


def test_new_export_requires_observation_count(db, db_engine, tmp_path):
    import gzip
    import json

    from garmin_ai.models import Base
    from garmin_ai.operations import REVISION, restore_database

    path = tmp_path / "truncated.gz"
    db.commit()
    with gzip.open(path, "wt") as output:
        output.write(json.dumps({"format": "garmin-ai-jsonl-v1", "revision": REVISION}) + "\n")
        output.write(
            json.dumps(
                {
                    "counts": {
                        name: 0 for name in Base.metadata.tables if name != "metric_observations"
                    }
                }
            )
            + "\n"
        )
    with pytest.raises(ValueError, match="Incomplete"):
        restore_database(db_engine, path)


def test_many_runs_use_one_temporal_query(db, db_engine):
    from sqlalchemy import event

    readiness(db, {"timestamp": instant(8).isoformat(), "score": 70})
    for index in range(30):
        db.add(
            Activity(
                id=f"run-{index}",
                kind="running",
                start=instant(10),
                end=instant(11),
                timezone="UTC",
                duration_seconds=3600,
                distance_m=10000,
                avg_hr=140,
            )
        )
    db.flush()
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        result = running_efficiency(db, instant(9), instant(12))
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)
    assert result["n"] == 30 and all(row["readiness"] == 70 for row in result["rows"])
    assert sum("FROM metric_observations" in sql for sql in statements) == 1
