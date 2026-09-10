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

    import json

    from garmin_ai.agent import AgentStep, ReadCall

    class Provider:
        calls = 0

        def structured(self, instruction, prompt, schema):
            data = json.loads(prompt)
            assert data["garmin_replay_notice"]
            assert data["quality_context"] == {}
            if data["tools"]:
                assert "data_freshness" in {tool["name"] for tool in data["tools"]}
            assert all(tool["name"] != "daily_summary" for tool in data["tools"])
            self.calls += 1
            if self.calls == 1:
                return AgentStep(
                    calls=[
                        ReadCall(
                            name="events",
                            arguments_json=json.dumps(
                                {
                                    "start": NOW.isoformat(),
                                    "end": (NOW + timedelta(days=1)).isoformat(),
                                }
                            ),
                        )
                    ]
                )
            return AgentStep(answer="Synthetic diary answer", evidence_ids=[1])

    assert "Synthetic diary answer" in answer_question(db, Provider(), "synthetic", Settings(), NOW)
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
    metadata = db.get(AppState, "ingest-meta:" + second["source_ref"])
    metadata.value = {**metadata.value, "failed_parser_version": PARSER_VERSION - 1}
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
        call_tool(
            db, "analysis_running_efficiency", {"start": NOW, "end": NOW + timedelta(hours=1)}
        )
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


@pytest.mark.parametrize("source_zone", [False, True])
def test_legacy_activity_page_recovers_per_activity_timezone(db, db_engine, tmp_path, source_zone):
    from garmin_ai.models import Activity

    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    entry = {"activityId": 999, "startTimeGMT": NOW.isoformat(), "duration": 60}
    if source_zone:
        entry["timeZoneUnitDTO"] = {"timeZone": "Asia/Tokyo"}
    result = ingest(db, archive, "activities", "0:20", [entry], "Europe/Budapest", fetched_at=NOW)
    raw = db.get(SourcePayload, UUID(result["source_ref"]))
    raw.parser_version = 0
    db.delete(db.get(AppState, f"ingest-meta:{raw.id}"))
    db.commit()
    run_replay(
        db_engine,
        archive,
        Settings(timezone="UTC"),
        {"account": ACCOUNT, "target_version": PARSER_VERSION, "raw_ref": str(raw.id)},
    )
    db.expire_all()
    assert db.get(Activity, "999").timezone == ("Asia/Tokyo" if source_zone else "Europe/Budapest")
    assert replay_status(db)["ready"]


@pytest.mark.parametrize(
    "name",
    [
        "health_snapshot",
        "health_range",
        "metric_series",
        "activities",
        "activity_details",
        "timeline",
        "insights_list",
    ],
)
def test_all_garmin_projection_tools_are_gated_before_argument_validation(db, tmp_path, name):
    from garmin_ai.tools import call_tool

    raw(db, LocalArchive(tmp_path), NOW)
    with pytest.raises(ValueError, match="пересчитываются"):
        call_tool(db, name, {})


def test_reapplied_revision_refreshes_timezone_but_unchanged_does_not(db, tmp_path):
    archive = LocalArchive(tmp_path)
    first = {"timestamp": NOW.isoformat(), "score": 70}
    result = ingest(db, archive, "readiness", "2026-09-10", first, "UTC", fetched_at=NOW)
    ingest(
        db,
        archive,
        "readiness",
        "2026-09-10",
        {**first, "score": 80},
        "UTC",
        fetched_at=NOW + timedelta(seconds=1),
    )
    ingest(
        db,
        archive,
        "readiness",
        "2026-09-10",
        first,
        "America/New_York",
        fetched_at=NOW + timedelta(seconds=2),
    )
    ingest(
        db,
        archive,
        "readiness",
        "2026-09-10",
        first,
        "Asia/Tokyo",
        fetched_at=NOW + timedelta(seconds=3),
    )
    db.expire_all()
    assert (
        db.get(AppState, "ingest-meta:" + result["source_ref"]).value["timezone"]
        == "America/New_York"
    )


def test_superseded_missing_archive_does_not_delay_current_revision(db, tmp_path):
    from garmin_ai.replay import replay_source

    bind_account(db, ACCOUNT)
    archive = LocalArchive(tmp_path)
    old = raw(db, archive, NOW)
    current = raw(db, archive, NOW + timedelta(seconds=1), value=90)
    db.add(
        AppState(
            key=f"ingest:{old.source}:{old.endpoint}:{old.source_key}",
            value={"source_ref": str(current.id), "requested_at": current.fetched_at.isoformat()},
        )
    )
    old.archive_key = "missing.json"
    db.flush()
    from garmin_ai.jobs import enqueue

    for index in range(99):
        enqueue(
            db, "raw_replay", {"target_version": PARSER_VERSION}, f"synthetic-existing:{index}", NOW
        )
    schedule_replay(db, NOW)
    assert (
        db.scalar(select(Job).where(Job.dedup_key == f"raw-replay:{current.id}:{PARSER_VERSION}"))
        is not None
    )
    assert (
        db.scalar(select(Job).where(Job.dedup_key == f"raw-replay:{old.id}:{PARSER_VERSION}"))
        is None
    )
    payload = {"raw_ref": str(old.id), "target_version": PARSER_VERSION}
    assert replay_source(db, archive, Settings(), payload)["status"] == "superseded_revision"


@pytest.mark.parametrize("status", ["sending", "sent", "uncertain"])
def test_replay_retires_delivered_context_without_erasing_history(db, tmp_path, status):
    from garmin_ai.models import PendingQuestion
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    row = raw(db, archive, NOW)
    question = PendingQuestion(
        kind="context",
        text="synthetic",
        evidence={"synthetic": True},
        priority=1,
        earliest_send_at=NOW,
        expires_at=NOW + timedelta(days=1),
        sent_at=NOW,
        status=status,
        dedup_key="synthetic-context",
    )
    db.add(question)
    db.flush()
    replay_source(
        db, archive, Settings(), {"raw_ref": str(row.id), "target_version": PARSER_VERSION}
    )
    db.refresh(question)
    assert (
        question.status == "cancelled"
        and question.sent_at == NOW
        and question.evidence == {"synthetic": True}
    )


@pytest.mark.parametrize("prior_success", [False, True])
def test_live_current_parser_failure_does_not_close_replay_readiness(
    db, tmp_path, monkeypatch, prior_success
):
    import importlib

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    first = None
    if prior_success:
        first = ingest(
            db,
            archive,
            "readiness",
            "2026-09-10",
            {"timestamp": NOW.isoformat(), "score": 10},
            "UTC",
            fetched_at=NOW,
        )

    def fail(*args):
        raise ValueError("synthetic")

    monkeypatch.setattr(module, "normalize", fail)
    failed = ingest(
        db,
        archive,
        "readiness",
        "2026-09-10",
        {"timestamp": NOW.isoformat(), "score": 20},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    assert failed["status"] == "error" and replay_status(db)["ready"]
    state = db.get(
        AppState, "ingest:garmin_connect:readiness:2026-09-10", populate_existing=True
    ).value
    assert state["latest_attempt"]["source_ref"] == failed["source_ref"]
    assert state.get("source_ref") == (first["source_ref"] if first else None)
    stale = ingest(db, archive, "readiness", "2026-09-10", {}, "UTC", fetched_at=NOW)
    assert stale["status"] == "stale"


def test_failed_upgrade_attempt_still_rebuilds_last_successful_projection(
    db, tmp_path, monkeypatch
):
    import importlib

    from garmin_ai.replay import replay_source

    module = importlib.import_module("garmin_ai.ingest")
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
    normalize = module.normalize

    def reject_new(session, endpoint, key, payload, ref, timezone):
        if payload.get("score") == 20:
            raise ValueError("synthetic")
        return normalize(session, endpoint, key, payload, ref, timezone)

    monkeypatch.setattr(module, "normalize", reject_new)
    failed = ingest(
        db,
        archive,
        "readiness",
        "2026-09-10",
        {"timestamp": NOW.isoformat(), "score": 20},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    metadata = db.get(AppState, "ingest-meta:" + failed["source_ref"], populate_existing=True)
    metadata.value = {**metadata.value, "failed_parser_version": PARSER_VERSION - 1}
    db.flush()
    assert not replay_status(db)["ready"]
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"target_version": PARSER_VERSION, "raw_ref": failed["source_ref"]},
        )["status"]
        == "error"
    )
    assert not replay_status(db)["ready"]
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"target_version": PARSER_VERSION, "raw_ref": first["source_ref"]},
        )["status"]
        == "normalized"
    )
    assert replay_status(db)["ready"]
    state = db.get(
        AppState, "ingest:garmin_connect:readiness:2026-09-10", populate_existing=True
    ).value
    assert state["latest_attempt"]["source_ref"] == failed["source_ref"]
    assert db.get(HealthDay, NOW.date()).training_readiness_score == 10


def test_unchanged_legacy_source_does_not_invent_timezone(db, tmp_path):
    archive = LocalArchive(tmp_path)
    payload = {"timestamp": NOW.isoformat(), "score": 70}
    result = ingest(db, archive, "readiness", "2026-09-10", payload, "UTC", fetched_at=NOW)
    key = "ingest-meta:" + result["source_ref"]
    db.delete(db.get(AppState, key))
    db.flush()
    ingest(
        db,
        archive,
        "readiness",
        "2026-09-10",
        payload,
        "Asia/Tokyo",
        fetched_at=NOW + timedelta(seconds=1),
    )
    assert db.get(AppState, key) is None


def test_replay_freshness_excludes_projection_channels(db, tmp_path):
    from garmin_ai.queries import data_freshness

    archive = LocalArchive(tmp_path)
    raw(db, archive, NOW)
    db.add(HealthDay(day=NOW.date(), training_readiness_score=95))
    db.flush()
    result = data_freshness(db, NOW)
    assert not result["archive_replay"]["ready"]
    assert not result["available"]
    assert result["channels"] == {}


@pytest.mark.parametrize("prior_success", [False, True])
def test_failed_fit_attempt_only_retries_after_parser_change(
    db, tmp_path, monkeypatch, prior_success
):
    import garmin_ai.fit as fit
    from garmin_ai.models import Activity, ActivityPart
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    activity = Activity(
        id="synthetic", start=NOW, end=NOW + timedelta(minutes=1), kind="running", timezone="UTC"
    )
    db.add(activity)
    db.flush()
    monkeypatch.setattr(fit, "extract_fit", lambda data: [data])

    def parse(data):
        if data == b"broken":
            raise ValueError("synthetic invalid FIT")
        return [("record", {"heart_rate": 70})]

    monkeypatch.setattr(fit, "parse_fit", parse)
    first = fit.store_fit(db, archive, activity.id, b"valid", NOW) if prior_success else None
    old_key = activity.fit_key
    failed = fit.store_fit(db, archive, activity.id, b"broken", NOW + timedelta(seconds=1))
    db.flush()
    assert failed["status"] == "error"
    assert activity.fit_key == old_key
    assert replay_status(db)["ready"]
    if first:
        assert db.scalar(select(ActivityPart)).payload["heart_rate"] == 70
    # Simulate a version transition: the failed attempt was made by the old parser.
    metadata = db.get(AppState, "ingest-meta:" + failed["source_ref"])
    metadata.value = {"failed_parser_version": PARSER_VERSION - 1}
    if first:
        db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    assert not replay_status(db)["ready"]
    result = replay_source(
        db, archive, Settings(), {"raw_ref": failed["source_ref"], "target_version": PARSER_VERSION}
    )
    assert result["status"] == "error"
    db.flush()
    if first:
        assert not replay_status(db)["ready"]
        assert (
            replay_source(
                db,
                archive,
                Settings(),
                {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
            )["status"]
            == "normalized"
        )
    db.flush()
    assert replay_status(db)["ready"]
    assert (
        db.get(AppState, "fit-version:synthetic").value["latest_attempt"]["source_ref"]
        == failed["source_ref"]
    )


def test_new_parser_can_promote_previously_failed_fit(db, tmp_path, monkeypatch):
    import garmin_ai.fit as fit
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    activity = Activity(
        id="synthetic", start=NOW, end=NOW + timedelta(minutes=1), kind="running", timezone="UTC"
    )
    db.add(activity)
    db.flush()
    monkeypatch.setattr(fit, "extract_fit", lambda data: [data])

    def fail(data):
        raise ValueError("synthetic")

    monkeypatch.setattr(fit, "parse_fit", fail)
    failed = fit.store_fit(db, archive, activity.id, b"synthetic", NOW)
    db.get(AppState, "ingest-meta:" + failed["source_ref"]).value = {
        "failed_parser_version": PARSER_VERSION - 1
    }
    db.flush()
    monkeypatch.setattr(fit, "parse_fit", lambda data: [("record", {"heart_rate": 70})])
    assert (
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": failed["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    db.flush()
    assert replay_status(db)["ready"]
    assert activity.fit_key == db.get(SourcePayload, UUID(failed["source_ref"])).archive_key
    assert "latest_attempt" not in db.get(AppState, "fit-version:synthetic").value


@pytest.mark.parametrize("fail", [False, True])
def test_parser_upgrade_rebuilds_temporal_projection_atomically(db, tmp_path, monkeypatch, fail):
    import importlib

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    payload = {"timestamp": NOW.isoformat(), "score": 70}
    first = ingest(db, archive, "readiness", "2026-09-10", payload, "UTC", fetched_at=NOW)
    raw = db.get(SourcePayload, UUID(first["source_ref"]))
    before = db.scalar(select(MetricObservation))
    old_id = before.id
    raw.parser_version = PARSER_VERSION - 1
    original = module.normalize

    def changed(session, endpoint, key, value, ref, timezone):
        result = original(session, endpoint, key, {**value, "score": 80}, ref, timezone)
        if fail:
            raise ValueError("synthetic parser failure")
        return result

    monkeypatch.setattr(module, "normalize", changed)
    result = ingest(
        db, archive, "readiness", "2026-09-10", payload, "UTC", fetched_at=NOW, replay=True
    )
    db.flush()
    rows = db.scalars(select(MetricObservation)).all()
    assert len(rows) == 1 and rows[0].fetched_at == NOW
    assert rows[0].value == (70 if fail else 80)
    assert (rows[0].id == old_id) == fail
    assert result["status"] == ("error" if fail else "normalized")


@pytest.mark.parametrize("fail", [False, True])
def test_zero_sample_replay_removes_owned_measurements_atomically(db, tmp_path, monkeypatch, fail):
    import importlib

    from garmin_ai.models import Measurement

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    payload = {"heartRateValues": [[int(NOW.timestamp() * 1000), 70]]}
    result = ingest(db, archive, "heart_rate", "2026-09-10", payload, "UTC", fetched_at=NOW)
    raw = db.get(SourcePayload, UUID(result["source_ref"]))
    assert db.scalar(select(Measurement.value)) == 70
    raw.parser_version = PARSER_VERSION - 1
    original = module.normalize

    def rejected(session, endpoint, key, value, ref, timezone):
        result = original(session, endpoint, key, {"heartRateValues": []}, ref, timezone)
        if fail:
            raise ValueError("synthetic parser failure")
        return result

    monkeypatch.setattr(module, "normalize", rejected)
    result = ingest(
        db, archive, "heart_rate", "2026-09-10", payload, "UTC", fetched_at=NOW, replay=True
    )
    db.flush()
    assert db.scalar(select(func.count()).select_from(Measurement)) == int(fail)
    assert result["status"] == ("error" if fail else "normalized")


@pytest.mark.parametrize("fail", [False, True])
def test_replay_clears_rejected_daily_and_sleep_projections_atomically(
    db, tmp_path, monkeypatch, fail
):
    import importlib
    from datetime import date

    from garmin_ai.models import HealthDay, TimelineInterval

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    payload = {
        "dailySleepDTO": {
            "sleepTimeSeconds": 3600,
            "sleepStartTimestampGMT": int(NOW.timestamp() * 1000),
            "sleepEndTimestampGMT": int((NOW + timedelta(hours=1)).timestamp() * 1000),
        }
    }
    result = ingest(db, archive, "sleep", "2026-09-10", payload, "UTC", fetched_at=NOW)
    raw = db.get(SourcePayload, UUID(result["source_ref"]))
    raw.parser_version = PARSER_VERSION - 1
    assert db.get(HealthDay, date(2026, 9, 10)).sleep_seconds == 3600
    assert db.get(TimelineInterval, "sleep:2026-09-10") is not None
    original = module.normalize

    def rejected(session, endpoint, key, value, ref, timezone):
        result = original(session, endpoint, key, {}, ref, timezone)
        if fail:
            raise ValueError("synthetic failure")
        return result

    monkeypatch.setattr(module, "normalize", rejected)
    ingest(db, archive, "sleep", "2026-09-10", payload, "UTC", fetched_at=NOW, replay=True)
    db.flush()
    db.expire_all()
    assert db.get(HealthDay, date(2026, 9, 10)).sleep_seconds == (3600 if fail else None)
    assert (db.get(TimelineInterval, "sleep:2026-09-10") is not None) == fail
