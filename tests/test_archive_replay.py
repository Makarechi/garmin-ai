from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from garmin_ai.accounts import AccountMismatch, bind_account, profile_fingerprint
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.ingest import ingest
from garmin_ai.models import AppState, HealthDay, Insight, Job, MetricObservation, SourcePayload
from garmin_ai.normalize import PARSER_VERSION
from garmin_ai.replay import replay_status, run_replay, schedule_replay

NOW = datetime(2026, 9, 10, tzinfo=UTC)
ACCOUNT = profile_fingerprint({"profileId": 12345})


def raw(db, archive, at, value=70):
    payload = {"timestamp": at.isoformat(), "score": value}
    key = archive.put_json(payload)
    row = SourcePayload(
        endpoint="readiness",
        source_key=str(at.date()),
        payload=payload,
        archive_key=key,
        payload_hash=key.split("/")[-1].split(".")[0],
        fetched_at=at,
        parser_version=0,
        status="archived",
    )
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize("insight_status", ["accepted", "uncertain"])
def test_two_year_archive_replays_offline_and_resumes_without_duplicates(
    db, db_engine, tmp_path, insight_status
):
    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    at = NOW - timedelta(days=730)
    raw(db, archive, at)
    db.add(
        Insight(
            category="synthetic",
            statement="synthetic",
            evidence={},
            sample_size=1,
            status=insight_status,
            dedup_key="test",
        )
    )
    schedule_replay(db, NOW)
    db.commit()
    db.expire_all()
    payload = dict(db.scalar(select(Job)).payload)
    db.commit()
    for _ in range(2):
        assert run_replay(db_engine, archive, Settings(timezone="UTC"), payload)["status"] in {
            "normalized",
            "unchanged",
        }
    db.expire_all()
    assert db.get(HealthDay, at.date()).training_readiness_score == 70
    observation = db.scalar(select(MetricObservation))
    assert observation.fetched_at == at
    assert observation.ingested_at > at
    assert db.scalar(select(func.count()).select_from(MetricObservation)) == 1
    assert db.scalar(select(Insight)).status == "superseded"
    schedule_replay(db, NOW)
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_replay_preserves_current_a_b_a_request_order(db, db_engine, tmp_path):
    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    refs = []
    for index, value in enumerate((10, 20, 10)):
        result = ingest(
            db,
            archive,
            "daily",
            "2024-09-10",
            {"totalSteps": value},
            "UTC",
            fetched_at=NOW + timedelta(seconds=index),
        )
        refs.append(result["source_ref"])
    for row in db.scalars(select(SourcePayload)):
        row.parser_version = 0
    schedule_replay(db, NOW)
    jobs = [dict(job.payload) for job in db.scalars(select(Job))]
    db.commit()
    outcomes = [
        run_replay(db_engine, archive, Settings(timezone="UTC"), payload)["status"]
        for payload in jobs
    ]
    db.expire_all()
    assert set(outcomes) == {"normalized", "superseded_revision"}
    assert refs[0] == refs[2]
    assert db.get(HealthDay, datetime(2024, 9, 10).date()).steps == 10
    assert replay_status(db)["outcomes"]["superseded_revision"] == 1


def test_hash_failure_keeps_raw_and_records_only_technical_error(db, db_engine, tmp_path):
    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    row = raw(db, archive, NOW)
    schedule_replay(db, NOW)
    payload = dict(db.scalar(select(Job)).payload)
    (tmp_path / row.archive_key).write_bytes(b"synthetic corrupt bytes")
    db.commit()
    with pytest.raises(RuntimeError, match="Offline replay"):
        run_replay(db_engine, archive, Settings(), payload)
    db.expire_all()
    assert db.get(SourcePayload, UUID(payload["raw_ref"])) is not None
    state = db.get(AppState, f"replay:{row.id}:{PARSER_VERSION}")
    assert state.value["error_type"] == "ValueError"
    assert "corrupt" not in str(state.value)
    assert db.scalar(select(HealthDay)) is None


def test_replay_planner_is_bounded_and_account_guard_is_enforced(db, db_engine, tmp_path):
    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    for days in range(105):
        raw(db, archive, NOW - timedelta(days=days))
    for _ in range(6):
        schedule_replay(db, NOW)
        db.flush()
    assert db.scalar(select(func.count()).select_from(Job)) == 100
    payload = dict(db.scalar(select(Job)).payload)
    payload["account"] = profile_fingerprint({"profileId": 99999})
    db.commit()
    with pytest.raises(AccountMismatch):
        run_replay(db_engine, archive, Settings(), payload)


def test_insight_claim_waits_between_replay_batches_and_after_failure(db, tmp_path):
    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.replay import replay_pending_condition

    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    for days in range(26):
        raw(db, archive, NOW - timedelta(days=days))
    enqueue(db, "agent_insights", {}, "synthetic-insight", NOW)
    # Even before scheduling, an old projection must not feed a new insight.
    assert claim(db, now=NOW, kinds=["agent_insights"]) is None
    schedule_replay(db, NOW)
    jobs = db.scalars(select(Job).where(Job.kind == "raw_replay")).all()
    assert len(jobs) == 25
    for job in jobs:
        job.status = "done"
    db.flush()
    assert claim(db, now=NOW, kinds=["agent_insights"]) is None
    schedule_replay(db, NOW)
    last = db.scalar(select(Job).where(Job.kind == "raw_replay", Job.status == "pending"))
    last.status = "failed"
    db.flush()
    assert db.scalar(select(replay_pending_condition()))
    assert claim(db, now=NOW, kinds=["agent_insights"]) is None
    last.status = "done"
    db.flush()
    assert not db.scalar(select(replay_pending_condition()))
    assert claim(db, now=NOW, kinds=["agent_insights"]).kind == "agent_insights"


def test_replay_planner_does_not_wait_behind_large_normalization(db, db_engine):
    from sqlalchemy import text

    from garmin_ai.db import transaction

    bind_account(db, ACCOUNT)
    db.commit()
    with db_engine.begin() as normalizer:
        normalizer.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        with transaction(db_engine) as scheduler:
            scheduler.execute(text("SET LOCAL statement_timeout = '1000ms'"))
            assert schedule_replay(scheduler, NOW) is None
            assert scheduler.scalar(select(func.count()).select_from(Job)) == 0
