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
        db.get(SourcePayload, UUID(job.payload["raw_ref"])).parser_version = PARSER_VERSION
    db.flush()
    assert claim(db, now=NOW, kinds=["agent_insights"]) is None
    schedule_replay(db, NOW)
    last = db.scalar(select(Job).where(Job.kind == "raw_replay", Job.status == "pending"))
    last.status = "failed"
    db.flush()
    assert db.scalar(select(replay_pending_condition()))
    assert claim(db, now=NOW, kinds=["agent_insights"]) is None
    last.status = "done"
    db.get(SourcePayload, UUID(last.payload["raw_ref"])).parser_version = PARSER_VERSION
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


def test_rollback_leaves_newer_parser_jobs_pending_until_redeployment(db, monkeypatch):
    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.replay import replay_source

    future = PARSER_VERSION + 1
    identity = enqueue(db, "raw_replay", {"target_version": future}, "future-parser", NOW)
    assert claim(db, now=NOW, kinds=["raw_replay"]) is None
    row = db.get(Job, identity)
    assert row.status == "pending" and row.attempts == 0
    with pytest.raises(ValueError, match="newer parser"):
        replay_source(db, None, Settings(), row.payload)
    assert row.status == "pending"
    monkeypatch.setattr("garmin_ai.normalize.PARSER_VERSION", future)
    assert claim(db, now=NOW, kinds=["raw_replay"]).id == identity


def test_upgrade_does_not_consume_old_target_before_rollback(db, monkeypatch):
    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.replay import replay_source

    target = PARSER_VERSION - 1
    identity = enqueue(db, "raw_replay", {"target_version": target}, "old-parser", NOW)
    assert claim(db, now=NOW, kinds=["raw_replay"]) is None
    row = db.get(Job, identity)
    assert row.status == "pending" and row.attempts == 0
    with pytest.raises(ValueError, match="matching parser"):
        replay_source(db, None, Settings(), row.payload)
    monkeypatch.setattr("garmin_ai.normalize.PARSER_VERSION", target)
    assert claim(db, now=NOW, kinds=["raw_replay"]).id == identity


def test_rollback_repairs_legacy_obsolete_completion_without_duplicate(db, tmp_path):
    from garmin_ai.replay import replay_pending_condition

    bind_account(db, ACCOUNT)
    source = raw(db, LocalArchive(tmp_path), NOW)
    schedule_replay(db, NOW)
    job = db.scalar(select(Job))
    identity = job.id
    job.status = "done"
    job.completed_at = NOW
    db.add(
        AppState(
            key=f"replay:{source.id}:{PARSER_VERSION}",
            value={"status": "obsolete_target", "target_version": PARSER_VERSION},
        )
    )
    db.flush()
    assert db.scalar(select(replay_pending_condition()))
    schedule_replay(db, NOW + timedelta(minutes=1))
    db.flush()
    assert job.id == identity and job.status == "pending" and job.attempts == 0
    assert job.completed_at is None
    assert db.scalar(select(func.count()).select_from(Job)) == 1


def test_context_claim_retains_replay_pause_but_diary_lane_remains_available(db, tmp_path):
    from garmin_ai.jobs import claim, enqueue

    bind_account(db, ACCOUNT)
    source = raw(db, LocalArchive(tmp_path), NOW)
    enqueue(db, "agent_proactive", {}, "context-during-replay", NOW)
    job = claim(db, now=NOW, kinds=["agent_proactive"])
    assert job is not None and job.payload["replay_pending"]
    source.parser_version = PARSER_VERSION
    db.flush()
    assert job.payload["replay_pending"]


def test_future_replay_queue_does_not_starve_current_parser(db, tmp_path):
    from garmin_ai.jobs import enqueue

    bind_account(db, ACCOUNT)
    raw(db, LocalArchive(tmp_path), NOW)
    for index in range(100):
        enqueue(db, "raw_replay", {"target_version": PARSER_VERSION + 1}, f"future:{index}", NOW)
    schedule_replay(db, NOW)
    current = db.scalars(
        select(Job).where(Job.payload["target_version"].as_integer() == PARSER_VERSION)
    ).all()
    assert len(current) == 1 and current[0].status == "pending"


def test_runtime_disables_context_generation_while_replay_is_pending(
    db, db_engine, tmp_path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    from garmin_ai import runtime

    bind_account(db, ACCOUNT)
    raw(db, LocalArchive(tmp_path / "raw"), NOW)
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="synthetic",
        telegram_user_id=42,
        timezone="UTC",
        proactive_enabled=True,
        quiet_start_hour=0,
        quiet_end_hour=0,
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)
    monkeypatch.setattr(runtime, "GeminiProvider", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("garmin_ai.replay.schedule_replay", lambda *args: None)
    seen = []

    async def scenario():
        ready = asyncio.Event()
        callbacks = []

        def generate(session, settings, now, *, allow_context):
            seen.append(allow_context)
            ready.set()

        class Bot:
            def __init__(self, *args):
                pass

            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            async def get_webhook_info(self):
                return SimpleNamespace(url="")

            async def get_updates(self, **kwargs):
                await asyncio.sleep(0.01)
                return []

            async def send_message(self, **kwargs):
                return SimpleNamespace(message_id=1)

        monkeypatch.setattr(runtime, "Bot", Bot)
        monkeypatch.setattr(runtime, "generate_questions", generate)
        monkeypatch.setattr(
            asyncio.get_running_loop(), "add_signal_handler", lambda s, cb: callbacks.append(cb)
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            await asyncio.wait_for(ready.wait(), 4)
            assert seen and not any(seen)
        finally:
            callbacks[0]()
            await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


@pytest.mark.parametrize("completed_before", [False, True])
def test_rollback_rebuilds_newer_canonical_projection(db, db_engine, tmp_path, completed_before):
    from garmin_ai.jobs import enqueue
    from garmin_ai.replay import replay_pending_condition

    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    row = raw(db, archive, NOW)
    row.parser_version = PARSER_VERSION + 1
    payload = {"raw_ref": str(row.id), "target_version": PARSER_VERSION, "account": ACCOUNT}
    if completed_before:
        identity = enqueue(db, "raw_replay", payload, f"raw-replay:{row.id}:{PARSER_VERSION}", NOW)
        job = db.get(Job, identity)
        job.status = "done"
        job.completed_at = NOW
        db.flush()
    assert db.scalar(select(replay_pending_condition()))
    schedule_replay(db, NOW)
    job = db.scalar(select(Job).where(Job.kind == "raw_replay"))
    assert job.status == "pending"
    db.commit()
    assert (
        run_replay(db_engine, archive, Settings(timezone="UTC"), payload)["status"] == "normalized"
    )
    db.expire_all()
    assert db.get(SourcePayload, row.id).parser_version == PARSER_VERSION
    assert not db.scalar(select(replay_pending_condition()))


def test_interactive_answers_wait_for_canonical_replay(db, db_engine, tmp_path):
    from garmin_ai.agent import answer_question
    from garmin_ai.replay import REPLAY_NOTICE
    from garmin_ai.telegram import process_message, save_update

    archive = LocalArchive(tmp_path)
    raw(db, archive, NOW)

    class Provider:
        def structured(self, *args):
            raise AssertionError("Must not send inconsistent Garmin evidence")

    assert answer_question(db, Provider(), "synthetic", Settings(), NOW) == REPLAY_NOTICE
    save_update(
        db,
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/today",
            },
        },
        42,
    )
    db.commit()
    assert process_message(db_engine, None, Settings(telegram_user_id=42), 1) == REPLAY_NOTICE


def test_status_explains_incomplete_replay(db, db_engine, tmp_path):
    from garmin_ai.telegram import process_message, save_update

    raw(db, LocalArchive(tmp_path), NOW)
    save_update(
        db,
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/status",
            },
        },
        42,
    )
    db.commit()
    assert "Пересчёт архива не завершён" in process_message(
        db_engine, None, Settings(telegram_user_id=42), 1
    )


def test_failed_rollback_job_retries_once_per_projection_version(db, tmp_path):
    bind_account(db, ACCOUNT)
    row = raw(db, LocalArchive(tmp_path), NOW)
    row.parser_version = PARSER_VERSION + 1
    schedule_replay(db, NOW)
    job = db.scalar(select(Job))
    job.status = "failed"
    db.flush()
    schedule_replay(db, NOW)
    assert job.status == "pending"
    job.status = "failed"
    db.flush()
    schedule_replay(db, NOW)
    assert job.status == "failed"
    row.parser_version += 1
    db.flush()
    schedule_replay(db, NOW)
    assert job.status == "pending"


def test_replay_keeps_original_timezone_after_setting_changes(db, db_engine, tmp_path):
    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    result = ingest(
        db,
        archive,
        "readiness",
        "2026-09-10",
        {"timestamp": NOW.isoformat(), "score": 78},
        "America/New_York",
        fetched_at=NOW,
    )
    row = db.get(SourcePayload, UUID(result["source_ref"]))
    row.parser_version = 0
    schedule_replay(db, NOW)
    payload = dict(db.scalar(select(Job)).payload)
    db.commit()
    run_replay(db_engine, archive, Settings(timezone="Asia/Tokyo"), payload)
    db.expire_all()
    zones = set(db.scalars(select(MetricObservation.timezone)))
    assert zones == {"America/New_York"}


def test_post_commit_retry_does_not_supersede_new_insight(db, db_engine, tmp_path):
    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    raw(db, archive, NOW)
    schedule_replay(db, NOW)
    payload = dict(db.scalar(select(Job)).payload)
    db.commit()
    run_replay(db_engine, archive, Settings(timezone="UTC"), payload)
    db.add(
        Insight(
            category="synthetic",
            statement="synthetic",
            evidence={},
            sample_size=1,
            status="accepted",
            dedup_key="after-replay",
        )
    )
    db.commit()
    assert (
        run_replay(db_engine, archive, Settings(timezone="UTC"), payload)["status"] == "unchanged"
    )
    assert db.scalar(select(Insight)).status == "accepted"


@pytest.mark.parametrize("legacy", [False, True])
def test_newest_failed_revision_is_replayed_before_old_success(
    db, db_engine, tmp_path, monkeypatch, legacy
):
    import importlib

    module = importlib.import_module("garmin_ai.ingest")
    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "readiness",
        "2026-09-10",
        {"timestamp": NOW.isoformat(), "score": 10},
        "UTC",
        fetched_at=NOW,
    )
    state = dict(db.get(AppState, "ingest:garmin_connect:readiness:2026-09-10").value)
    original = module.normalize

    def fail(*args):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(module, "normalize", fail)
    second = ingest(
        db,
        archive,
        "readiness",
        "2026-09-10",
        {"timestamp": NOW.isoformat(), "score": 20},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    assert second["status"] == "error"
    monkeypatch.setattr(module, "normalize", original)
    if legacy:
        db.get(AppState, "ingest:garmin_connect:readiness:2026-09-10").value = state
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = 0
    db.flush()
    assert not replay_status(db)["ready"]
    db.commit()
    payload = {"account": ACCOUNT, "target_version": PARSER_VERSION, "raw_ref": first["source_ref"]}
    assert (
        run_replay(db_engine, archive, Settings(timezone="UTC"), payload)["status"]
        == "superseded_revision"
    )
    payload["raw_ref"] = second["source_ref"]
    assert (
        run_replay(db_engine, archive, Settings(timezone="UTC"), payload)["status"] == "normalized"
    )
    db.expire_all()
    assert db.get(HealthDay, NOW.date()).training_readiness_score == 20
    assert replay_status(db)["ready"]


def test_date_keyed_legacy_daily_replays_without_timezone_metadata(db, db_engine, tmp_path):
    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    result = ingest(
        db, archive, "daily", str(NOW.date()), {"totalSteps": 123}, "UTC", fetched_at=NOW
    )
    row = db.get(SourcePayload, UUID(result["source_ref"]))
    row.parser_version = 0
    db.delete(db.get(AppState, f"ingest-meta:{row.id}"))
    payload = {"account": ACCOUNT, "target_version": PARSER_VERSION, "raw_ref": str(row.id)}
    db.commit()
    assert (
        run_replay(db_engine, archive, Settings(timezone="Asia/Tokyo"), payload)["status"]
        == "normalized"
    )
    db.expire_all()
    assert db.get(HealthDay, NOW.date()).steps == 123
    assert replay_status(db)["ready"]


def test_shared_analysis_tool_gate_preserves_diary_and_status_reads(db, tmp_path):
    from garmin_ai.tools import call_tool

    row = raw(db, LocalArchive(tmp_path), NOW)
    with pytest.raises(ValueError, match="пересчитываются"):
        call_tool(
            db,
            "personal_baseline",
            {"metric": "sleep_score", "start": str(NOW.date()), "end": str(NOW.date())},
        )
    with pytest.raises(ValueError, match="пересчитываются"):
        call_tool(db, "analysis_running_efficiency", {"start": NOW, "end": NOW + timedelta(hours=1)})
    assert call_tool(db, "events", {"start": NOW, "end": NOW + timedelta(hours=1)})["rows"] == []
    assert not call_tool(db, "data_freshness", {})["archive_replay"]["ready"]
    row.parser_version = PARSER_VERSION
    db.flush()
    assert (
        call_tool(
            db,
            "personal_baseline",
            {"metric": "sleep_score", "start": str(NOW.date()), "end": str(NOW.date())},
        )["n"]
        == 0
    )
