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


@pytest.mark.parametrize("recovered", [False, True])
def test_runtime_disables_context_generation_while_replay_is_pending(
    db, db_engine, tmp_path, monkeypatch, recovered
):
    import asyncio
    from types import SimpleNamespace

    from garmin_ai import runtime

    bind_account(db, ACCOUNT)
    archived = raw(db, LocalArchive(tmp_path / "raw"), NOW)
    if recovered:
        archived.parser_version = PARSER_VERSION
        original_claim = runtime.claim

        def stale_claim(*args, **kwargs):
            job = original_claim(*args, **kwargs)
            if job and job.kind == "agent_proactive":
                job.payload = {**job.payload, "replay_pending": True}
            return job

        monkeypatch.setattr(runtime, "claim", stale_claim)
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
            def __init__(self, *args, **kwargs):
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
            assert seen and all(value == recovered for value in seen)
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


def test_interactive_answers_wait_for_canonical_replay(db, db_engine, tmp_path, monkeypatch):
    from garmin_ai.agent import answer_question
    from garmin_ai.replay import REPLAY_NOTICE
    from garmin_ai.telegram import process_message, save_update

    archive = LocalArchive(tmp_path)
    raw(db, archive, NOW)
    import garmin_ai.conversation as conversation

    monkeypatch.setattr(
        conversation,
        "conversation_context",
        lambda *args, **kwargs: {
            "epoch": None,
            "selection_missing": False,
            "turns": [
                {"answer": "stale synthetic health answer", "tools": [{"name": "daily_summary"}]}
            ],
        },
    )

    import json

    from garmin_ai.agent import AgentStep, ReadCall

    class Provider:
        calls = 0

        def structured(self, instruction, prompt, schema):
            data = json.loads(prompt)
            assert data["garmin_replay_notice"]
            assert data["quality_context"] == {}
            assert data["conversation"]["turns"] == []
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
        run_replay(db_engine, archive, Settings(timezone="UTC"), payload)["status"] == "normalized"
    )
    payload["raw_ref"] = second["source_ref"]
    assert (
        run_replay(db_engine, archive, Settings(timezone="UTC"), payload)["status"] == "normalized"
    )
    db.expire_all()
    assert db.get(HealthDay, NOW.date()).training_readiness_score == 20
    assert replay_status(db)["ready"]


def test_failed_reparse_of_installed_fit_keeps_readiness_blocked(db, tmp_path, monkeypatch):
    import garmin_ai.fit as fit
    from garmin_ai.models import Activity, ActivityPart
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    db.add(
        Activity(
            id="synthetic-failure",
            start=NOW,
            end=NOW + timedelta(minutes=1),
            kind="running",
            timezone="UTC",
        )
    )
    db.flush()
    monkeypatch.setattr(fit, "extract_fit", lambda data: [data])
    monkeypatch.setattr(fit, "parse_fit", lambda data: [("record", {"heart_rate": 70})])
    result = fit.store_fit(db, archive, "synthetic-failure", b"synthetic", NOW)
    db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()

    def fail(data):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(fit, "parse_fit", fail)
    assert (
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "error"
    )
    db.flush()
    assert db.scalar(select(ActivityPart)).payload["heart_rate"] == 70
    assert not replay_status(db)["ready"]


@pytest.mark.parametrize("newest_first", [False, True])
def test_partial_daily_response_replays_every_retained_owner(db, tmp_path, newest_first):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "daily",
        str(NOW.date()),
        {"totalSteps": 100, "restingHeartRate": 60},
        "UTC",
        fetched_at=NOW,
    )
    latest = ingest(
        db,
        archive,
        "daily",
        str(NOW.date()),
        {"totalSteps": 200},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    for result in (first, latest):
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    assert not replay_status(db)["ready"]
    results = (latest, first) if newest_first else (first, latest)
    for index, result in enumerate(results):
        assert (
            replay_source(
                db,
                archive,
                Settings(timezone="UTC"),
                {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
            )["status"]
            == "normalized"
        )
        db.flush()
        assert replay_status(db)["ready"] == (index == 1)
    db.expire_all()
    day = db.get(HealthDay, NOW.date())
    assert day.steps == 200 and day.resting_hr == 60
    state = db.get(AppState, "ingest:garmin_connect:daily:" + str(NOW.date()))
    assert state.value["source_ref"] == latest["source_ref"]


@pytest.mark.parametrize("newest_first", [False, True])
def test_displaced_activity_page_remains_replayable(db, tmp_path, newest_first):
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    results = []
    for identity in (1, 2):
        result = ingest(
            db,
            archive,
            "activities",
            "0",
            [
                {
                    "activityId": identity,
                    "startTimeGMT": NOW.isoformat(),
                    "duration": 60,
                    "averageHR": 70 + identity,
                }
            ],
            "UTC",
            fetched_at=NOW + timedelta(minutes=identity),
        )
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
        results.append(result)
    db.flush()
    for index, result in enumerate(reversed(results) if newest_first else results):
        assert not replay_status(db)["ready"]
        assert (
            replay_source(
                db,
                archive,
                Settings(timezone="UTC"),
                {
                    "raw_ref": result["source_ref"],
                    "target_version": PARSER_VERSION,
                },
            )["status"]
            == "normalized"
        )
        db.flush()
        assert replay_status(db)["ready"] == (index == 1)
    assert db.get(Activity, "1").avg_hr == 71
    assert db.get(Activity, "2").avg_hr == 72


@pytest.mark.parametrize("endpoint", ["heart_rate", "sleep"])
def test_old_field_owner_cannot_replace_new_samples_or_sleep(db, tmp_path, endpoint):
    from garmin_ai.models import Measurement, TimelineInterval
    from garmin_ai.replay import replay_source

    stamp = int(NOW.timestamp() * 1000)
    if endpoint == "heart_rate":
        a = {"restingHeartRate": 60, "heartRateValues": [[stamp, 70]]}
        b = {"heartRateValues": [[stamp, 90]]}
    else:
        a = {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": stamp,
                "sleepEndTimestampGMT": stamp + 28800000,
                "sleepScores": {"overall": {"value": 70}},
            }
        }
        b = {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": stamp + 3600000,
                "sleepEndTimestampGMT": stamp + 32400000,
            }
        }
    archive = LocalArchive(tmp_path)
    first = ingest(db, archive, endpoint, str(NOW.date()), a, "UTC", fetched_at=NOW)
    last = ingest(
        db, archive, endpoint, str(NOW.date()), b, "UTC", fetched_at=NOW + timedelta(minutes=1)
    )
    for result in (first, last):
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for result in (last, first):
        assert (
            replay_source(
                db,
                archive,
                Settings(timezone="UTC"),
                {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
            )["status"]
            == "normalized"
        )
    if endpoint == "heart_rate":
        assert list(db.scalars(select(Measurement.value))) == [90]
    else:
        assert db.scalar(select(TimelineInterval)).start == NOW + timedelta(hours=1)
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
    assert "timezone" not in db.get(AppState, key).value


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
@pytest.mark.parametrize("legacy", [False, True])
def test_failed_fit_attempt_only_retries_after_parser_change(
    db, tmp_path, monkeypatch, prior_success, legacy
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
    if legacy:
        state = db.get(AppState, "fit-version:synthetic")
        state.value = {"requested_at": (NOW + timedelta(seconds=1)).isoformat()}
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
    db.expire_all()
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
    monkeypatch.setattr(
        importlib.import_module("garmin_ai.projection_history"), "normalize", rejected
    )
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


@pytest.mark.parametrize("fail", [False, True])
def test_activity_replay_clears_rejected_optional_fields_atomically(
    db, tmp_path, monkeypatch, fail
):
    import importlib

    from garmin_ai.models import Activity

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    payload = {
        "activityId": 701,
        "startTimeGMT": NOW.isoformat(),
        "duration": 600,
        "averageHR": 150,
        "distance": 2000,
    }
    result = ingest(db, archive, "activity", "701", payload, "UTC", fetched_at=NOW)
    row = db.get(SourcePayload, UUID(result["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    original = module.normalize

    def rejected(session, endpoint, key, value, ref, timezone):
        result = original(
            session,
            endpoint,
            key,
            {**value, "averageHR": "invalid", "distance": None},
            ref,
            timezone,
        )
        if fail:
            raise ValueError("synthetic failure")
        return result

    monkeypatch.setattr(module, "normalize", rejected)
    result = ingest(db, archive, "activity", "701", payload, "UTC", fetched_at=NOW, replay=True)
    db.expire_all()
    assert db.get(Activity, "701").avg_hr == (150 if fail else None)
    assert db.get(Activity, "701").distance_m == (2000 if fail else None)
    assert result["status"] == ("error" if fail else "normalized")


@pytest.mark.parametrize("status", ["accepted", "superseded"])
def test_insight_delivery_refetches_status_and_holds_normalization_lock(
    db, db_engine, monkeypatch, status
):
    import asyncio

    from sqlalchemy import text

    from garmin_ai import runtime

    row = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status=status,
        dedup_key="test:synthetic",
    )
    db.add(row)
    db.commit()
    identity = row.id
    db.commit()
    monkeypatch.setattr(runtime, "reserve_insight_notice", lambda *args: True)
    sent = []

    async def send(*args):
        with db_engine.connect() as probe:
            assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104619)"))
        sent.append(args[-1])

    monkeypatch.setattr(runtime, "deliver", send)
    asyncio.run(runtime.deliver_current_insight(None, db_engine, Settings(), identity))
    db.expire_all()
    assert sent == (["synthetic"] if status == "accepted" else [])
    assert db.get(Insight, identity).status == ("delivered" if status == "accepted" else status)
    db.commit()
    with db_engine.connect() as probe:
        assert probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104619)"))


def test_replay_promotes_old_failure_without_losing_newer_attempt(db, tmp_path, monkeypatch):
    import importlib

    from garmin_ai.replay import replay_source

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    original = module.normalize

    def normalize(session, endpoint, key, payload, ref, timezone):
        if payload.get("score") in {20, 30}:
            raise ValueError("synthetic failure")
        return original(session, endpoint, key, payload, ref, timezone)

    monkeypatch.setattr(module, "normalize", normalize)
    refs = []
    for i, score in enumerate([10, 20, 30]):
        refs.append(
            ingest(
                db,
                archive,
                "readiness",
                str(NOW.date()),
                {"timestamp": NOW.isoformat(), "score": score},
                "UTC",
                fetched_at=NOW + timedelta(minutes=i),
            )["source_ref"]
        )
    metadata = db.get(AppState, "ingest-meta:" + refs[1], populate_existing=True)
    metadata.value = {**metadata.value, "failed_parser_version": PARSER_VERSION - 1}
    db.flush()
    monkeypatch.setattr(module, "normalize", original)
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": refs[1], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    state = db.get(
        AppState, "ingest:garmin_connect:readiness:" + str(NOW.date()), populate_existing=True
    ).value
    assert state["source_ref"] == refs[1]
    assert state["latest_attempt"]["source_ref"] == refs[2]
    assert state["latest_attempt"]["requested_at"] == (NOW + timedelta(minutes=2)).isoformat()
    assert (
        ingest(
            db,
            archive,
            "readiness",
            str(NOW.date()),
            {"timestamp": NOW.isoformat(), "score": 40},
            "UTC",
            fetched_at=NOW + timedelta(seconds=90),
        )["status"]
        == "stale"
    )
    assert db.get(HealthDay, NOW.date()).training_readiness_score == 20


def test_legacy_sleep_without_score_recovers_date_timezone(db, tmp_path):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    result = ingest(
        db,
        archive,
        "sleep",
        str(NOW.date()),
        {
            "dailySleepDTO": {
                "sleepTimeSeconds": 3600,
                "sleepStartTimestampGMT": int(NOW.timestamp() * 1000),
                "sleepEndTimestampGMT": int((NOW + timedelta(hours=1)).timestamp() * 1000),
            }
        },
        "UTC",
        fetched_at=NOW,
    )
    row = db.get(SourcePayload, UUID(result["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    db.delete(db.get(AppState, "ingest-meta:" + result["source_ref"]))
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="Europe/Bratislava"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    assert db.get(HealthDay, NOW.date()).sleep_seconds == 3600
    assert replay_status(db)["ready"]


@pytest.mark.parametrize("legacy", [False, True])
def test_empty_fetch_keeps_retained_projection_eligible_for_replay(
    db, tmp_path, monkeypatch, legacy
):
    import importlib

    from garmin_ai.replay import replay_source

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "readiness",
        str(NOW.date()),
        {"timestamp": NOW.isoformat(), "score": 70},
        "UTC",
        fetched_at=NOW,
    )
    empty = ingest(
        db, archive, "readiness", str(NOW.date()), {}, "UTC", fetched_at=NOW + timedelta(minutes=1)
    )
    row = db.get(SourcePayload, UUID(first["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    state = db.get(
        AppState, "ingest:garmin_connect:readiness:" + str(NOW.date()), populate_existing=True
    )
    if legacy:
        empty_row = db.get(SourcePayload, UUID(empty["source_ref"]))
        state.value = {
            "source_ref": empty["source_ref"],
            "hash": empty_row.payload_hash,
            "requested_at": (NOW + timedelta(minutes=1)).isoformat(),
            "status": "empty",
        }
    else:
        assert state.value["source_ref"] == first["source_ref"]
        assert state.value["latest_attempt"]["source_ref"] == empty["source_ref"]
    db.flush()
    assert not replay_status(db)["ready"]
    original = module.normalize
    monkeypatch.setattr(
        module,
        "normalize",
        lambda session, endpoint, key, payload, ref, timezone: original(
            session, endpoint, key, {}, ref, timezone
        ),
    )
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "empty"
    )
    db.expire_all()
    assert db.get(HealthDay, NOW.date()).training_readiness_score is None
    assert replay_status(db)["ready"]


@pytest.mark.parametrize("newest_first", [False, True])
def test_activity_replay_keeps_omitted_field_owner(db, tmp_path, newest_first):
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    base = {"activityId": 801, "startTimeGMT": NOW.isoformat(), "duration": 60}
    first = ingest(
        db, archive, "activity", "801", {**base, "averageHR": 150}, "UTC", fetched_at=NOW
    )
    last = ingest(
        db,
        archive,
        "activity",
        "801",
        {**base, "duration": 90},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    for result in (first, last):
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for result in (last, first) if newest_first else (first, last):
        assert (
            replay_source(
                db,
                archive,
                Settings(timezone="UTC"),
                {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
            )["status"]
            == "normalized"
        )
    db.expire_all()
    assert db.get(Activity, "801").avg_hr == 150
    assert db.get(Activity, "801").duration_seconds == 90


def test_repeated_displaced_daily_raw_keeps_latest_application_time(db, tmp_path):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    results = []
    for minute, payload in enumerate(
        [
            {"totalSteps": 100, "restingHeartRate": 60},
            {"totalSteps": 200, "restingHeartRate": 70, "totalKilocalories": 2000},
            {"totalSteps": 100, "restingHeartRate": 60},
            {"totalSteps": 300},
        ]
    ):
        results.append(
            ingest(
                db,
                archive,
                "daily",
                str(NOW.date()),
                payload,
                "UTC",
                fetched_at=NOW + timedelta(minutes=minute),
            )
        )
    for result in results:
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for result in [results[0], results[1], results[3]]:
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )
    db.expire_all()
    assert db.get(HealthDay, NOW.date()).resting_hr == 60


@pytest.mark.parametrize("endpoint", ["activity", "activities"])
def test_failed_activity_revision_does_not_own_existing_activity(db, tmp_path, endpoint):
    archive = LocalArchive(tmp_path)
    base = {"activityId": 802, "startTimeGMT": NOW.isoformat(), "duration": 60}
    wrap = (lambda value: [value]) if endpoint == "activities" else (lambda value: value)
    ingest(db, archive, endpoint, "802", wrap(base), "UTC", fetched_at=NOW)
    result = ingest(
        db,
        archive,
        endpoint,
        "802",
        wrap({**base, "duration": "invalid"}),
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    assert result["status"] == "error"
    db.flush()
    assert replay_status(db)["ready"]


@pytest.mark.parametrize("newest_first", [False, True])
def test_legacy_activity_ownership_rebuilds_old_parser_field(
    db, tmp_path, monkeypatch, newest_first
):
    import garmin_ai.normalize as module
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    base = {"activityId": 811, "startTimeGMT": NOW.isoformat(), "duration": 60}
    first = ingest(
        db, archive, "activity", "811", {**base, "averageHR": 150}, "UTC", fetched_at=NOW
    )
    last = ingest(
        db,
        archive,
        "activity",
        "811",
        {**base, "duration": 90},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    state = db.get(AppState, "activity-version:811")
    state.value = {"requested_at": state.value["requested_at"]}
    for result in (first, last):
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value == 150 else original(value, **kw)
    )
    for result in (last, first) if newest_first else (first, last):
        assert (
            replay_source(
                db,
                archive,
                Settings(timezone="UTC"),
                {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
            )["status"]
            == "normalized"
        )
    db.expire_all()
    assert db.get(Activity, "811").avg_hr is None
    assert db.get(Activity, "811").duration_seconds == 90


@pytest.mark.parametrize("attested", [False, True])
def test_retained_replay_admits_new_nonconflicting_sample(db, tmp_path, monkeypatch, attested):
    import garmin_ai.normalize as module
    from garmin_ai.models import Measurement
    from garmin_ai.reconciliation import Replacement
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    t1, t2 = int(NOW.timestamp() * 1000), int((NOW + timedelta(minutes=1)).timestamp() * 1000)
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value == 77 else original(value, **kw)
    )
    first = ingest(
        db,
        archive,
        "heart_rate",
        str(NOW.date()),
        {"restingHeartRate": 60, "heartRateValues": [[t1, 77], [t2, 70]]},
        "UTC",
        fetched_at=NOW,
    )
    ingest(
        db,
        archive,
        "heart_rate",
        str(NOW.date()),
        {"heartRateValues": [[t2, 90]]},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
        replacement=Replacement(NOW, NOW + timedelta(minutes=2), ("heart_rate_bpm",), "synthetic")
        if attested
        else None,
    )
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    monkeypatch.setattr(module, "numeric", original)
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    assert list(db.scalars(select(Measurement.value).order_by(Measurement.ts))) == (
        [90] if attested else [77, 90]
    )


@pytest.mark.parametrize("with_samples", [False, True])
def test_legacy_hrv_summary_timezone_fallback(db, tmp_path, with_samples):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    payload = {"hrvSummary": {"lastNightAvg": 50}}
    if with_samples:
        payload["hrvReadings"] = [{"readingTimeGMT": NOW.isoformat(), "hrvValue": 50}]
    result = ingest(db, archive, "hrv", str(NOW.date()), payload, "UTC", fetched_at=NOW)
    db.delete(db.get(AppState, "ingest-meta:" + result["source_ref"]))
    db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    request = {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION}
    if with_samples:
        with pytest.raises(ValueError, match="timezone"):
            replay_source(db, archive, Settings(timezone="UTC"), request)
    else:
        assert (
            replay_source(db, archive, Settings(timezone="UTC"), request)["status"] == "normalized"
        )


def test_replay_tools_http_response_is_retryable(db, db_engine, tmp_path):
    from fastapi.testclient import TestClient

    from garmin_ai.api import create_app

    raw(db, LocalArchive(tmp_path), NOW)
    db.commit()
    client = TestClient(
        create_app(Settings(api_key="synthetic-test-api-key-32-characters"), db_engine)
    )
    response = client.post(
        "/tools/personal_baseline",
        headers={"Authorization": "Bearer synthetic-test-api-key-32-characters"},
        json={
            "arguments": {"metric": "sleep_score", "start": str(NOW.date()), "end": str(NOW.date())}
        },
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"


def test_retained_activity_replay_admits_previously_unowned_field(db, tmp_path, monkeypatch):
    import garmin_ai.normalize as module
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    base = {"activityId": 821, "startTimeGMT": NOW.isoformat(), "duration": 60}
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value == 1234 else original(value, **kw)
    )
    first = ingest(
        db,
        archive,
        "activity",
        "821",
        {**base, "averageHR": 150, "distance": 1234},
        "UTC",
        fetched_at=NOW,
    )
    ingest(
        db,
        archive,
        "activity",
        "821",
        {**base, "duration": 90},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    monkeypatch.setattr(module, "numeric", original)
    replay_source(
        db,
        archive,
        Settings(timezone="UTC"),
        {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
    )
    db.expire_all()
    assert db.get(Activity, "821").distance_m == 1234
    assert db.get(Activity, "821").duration_seconds == 90


def test_retained_sleep_replay_admits_new_interval(db, tmp_path, monkeypatch):
    import garmin_ai.normalize as module
    from garmin_ai.models import TimelineInterval
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    original = module.upsert

    def old_upsert(session, model, values, keys):
        if model is not TimelineInterval:
            original(session, model, values, keys)

    monkeypatch.setattr(module, "upsert", old_upsert)
    stamp = int(NOW.timestamp() * 1000)
    first = ingest(
        db,
        archive,
        "sleep",
        str(NOW.date()),
        {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": stamp,
                "sleepEndTimestampGMT": stamp + 28800000,
                "sleepScores": {"overall": {"value": 70}},
            }
        },
        "UTC",
        fetched_at=NOW,
    )
    ingest(
        db,
        archive,
        "sleep",
        str(NOW.date()),
        {"dailySleepDTO": {"sleepTimeSeconds": 100}},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    monkeypatch.setattr(module, "upsert", original)
    replay_source(
        db,
        archive,
        Settings(timezone="UTC"),
        {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
    )
    assert db.get(TimelineInterval, "sleep:" + str(NOW.date())) is not None


def test_failed_retained_replay_preserves_newer_success_watermark(db, tmp_path, monkeypatch):
    import importlib

    from garmin_ai.replay import replay_source

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "daily",
        str(NOW.date()),
        {"totalSteps": 100, "restingHeartRate": 60},
        "UTC",
        fetched_at=NOW,
    )
    ingest(
        db,
        archive,
        "daily",
        str(NOW.date()),
        {"totalSteps": 200},
        "UTC",
        fetched_at=NOW + timedelta(minutes=2),
    )
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    original = module.normalize

    def fail(*args):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(module, "normalize", fail)
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "error"
    )
    monkeypatch.setattr(module, "normalize", original)
    assert (
        ingest(
            db,
            archive,
            "daily",
            str(NOW.date()),
            {"totalSteps": 150},
            "UTC",
            fetched_at=NOW + timedelta(minutes=1),
        )["status"]
        == "stale"
    )


def test_failed_legacy_activity_owner_keeps_replay_gate_closed(db, tmp_path, monkeypatch):
    import importlib

    from garmin_ai.replay import replay_source

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    base = {"activityId": 831, "startTimeGMT": NOW.isoformat(), "duration": 60}
    first = ingest(
        db, archive, "activity", "831", {**base, "averageHR": 150}, "UTC", fetched_at=NOW
    )
    ingest(
        db,
        archive,
        "activity",
        "831",
        {**base, "duration": 90},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    state = db.get(AppState, "activity-version:831", populate_existing=True)
    state.value = {"requested_at": state.value["requested_at"]}
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()

    def fail(*args):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(module, "normalize", fail)
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "error"
    )
    db.flush()
    assert not replay_status(db)["ready"]


def test_legacy_activity_owner_maps_are_materialized_in_one_batch(db, tmp_path):
    from garmin_ai.normalize import legacy_activity_owners

    archive = LocalArchive(tmp_path)
    for identity in (841, 842):
        ingest(
            db,
            archive,
            "activity",
            str(identity),
            {
                "activityId": identity,
                "startTimeGMT": NOW.isoformat(),
                "duration": 60,
                "averageHR": 150,
            },
            "UTC",
            fetched_at=NOW,
        )
        state = db.get(AppState, f"activity-version:{identity}", populate_existing=True)
        state.value = {"requested_at": state.value["requested_at"]}
    db.flush()
    legacy_activity_owners(db, "841")
    assert db.get(AppState, "activity-version:842").value["owners"]["avg_hr"]
    assert db.get(AppState, "activity-version:842").value["owners_initialized"]


def test_legacy_summary_only_heart_rate_replays(db, tmp_path):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    result = ingest(
        db, archive, "heart_rate", str(NOW.date()), {"restingHeartRate": 60}, "UTC", fetched_at=NOW
    )
    db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.delete(db.get(AppState, "ingest-meta:" + result["source_ref"]))
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )


@pytest.mark.parametrize("newest_first", [False, True])
def test_newer_activity_replay_replaces_older_field_owner(db, tmp_path, monkeypatch, newest_first):
    import garmin_ai.normalize as module
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    base = {"activityId": 851, "startTimeGMT": NOW.isoformat(), "duration": 60}
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value == 151 else original(value, **kw)
    )
    results = [
        ingest(
            db,
            archive,
            "activity",
            "851",
            {**base, "averageHR": value},
            "UTC",
            fetched_at=NOW + timedelta(minutes=i),
        )
        for i, value in enumerate([150, 151])
    ]
    for result in results:
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    monkeypatch.setattr(module, "numeric", original)
    for result in reversed(results) if newest_first else results:
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )
    db.expire_all()
    assert db.get(Activity, "851").avg_hr == 151


def test_older_activity_replay_rebuilds_owned_name(db, tmp_path):
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    base = {"activityId": 852, "startTimeGMT": NOW.isoformat(), "duration": 60}
    first = ingest(
        db,
        archive,
        "activity",
        "852",
        {**base, "activityName": "Correct name"},
        "UTC",
        fetched_at=NOW,
    )
    ingest(
        db,
        archive,
        "activity",
        "852",
        {**base, "duration": 90},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    db.get(Activity, "852").name = "Old parser name"
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    replay_source(
        db,
        archive,
        Settings(timezone="UTC"),
        {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
    )
    db.expire_all()
    assert db.get(Activity, "852").name == "Correct name"
    assert db.get(Activity, "852").duration_seconds == 90


@pytest.mark.parametrize(
    "endpoint,array",
    [
        ("stress", "stressValuesArray"),
        ("respiration", "respirationValuesArray"),
        ("spo2", "spO2ValuesArray"),
    ],
)
@pytest.mark.parametrize("empty", [False, True, "no_timestamp"])
def test_projection_free_legacy_sample_timezone(db, tmp_path, endpoint, array, empty):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    result = ingest(
        db,
        archive,
        endpoint,
        str(NOW.date()),
        {array: [[None, 70]]}
        if empty == "no_timestamp"
        else ({array: []} if empty else {"ignored": True}),
        "UTC",
        fetched_at=NOW,
    )
    db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.delete(db.get(AppState, "ingest-meta:" + result["source_ref"]))
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )


def test_replay_finished_during_model_call_rejects_answer(db, tmp_path):
    from garmin_ai.agent import AgentStep, answer_question
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    result = ingest(
        db, archive, "daily", str(NOW.date()), {"totalSteps": 100}, "UTC", fetched_at=NOW
    )

    class Provider:
        def structured(self, *args):
            db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
            db.flush()
            replay_source(
                db,
                archive,
                Settings(timezone="UTC"),
                {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
            )
            return AgentStep(answer="Stale answer", evidence_ids=[1])

    assert "пересчитаны во время анализа" in answer_question(
        db, Provider(), "synthetic", Settings(), NOW
    )


def test_initialized_activity_drops_obsolete_page_from_gate(db, tmp_path):
    from garmin_ai.replay import canonical_source

    archive = LocalArchive(tmp_path)
    base = {"activityId": 861, "startTimeGMT": NOW.isoformat(), "duration": 60}
    first = ingest(db, archive, "activities", "0", [base], "UTC", fetched_at=NOW)
    ingest(
        db,
        archive,
        "activity",
        "861",
        {**base, "duration": 90},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    old = db.get(SourcePayload, UUID(first["source_ref"]))
    old.parser_version = PARSER_VERSION - 1
    old.status = "error"
    # The page watermark must also point at a newer page revision.
    ingest(
        db,
        archive,
        "activities",
        "0",
        [{**base, "duration": 100}],
        "UTC",
        fetched_at=NOW + timedelta(minutes=2),
    )
    db.flush()
    assert (
        db.scalar(select(SourcePayload.id).where(SourcePayload.id == old.id, canonical_source()))
        is None
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_activity_retains_valid_fields_and_name(db, tmp_path, reverse):
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    base = {"activityId": 862, "startTimeGMT": NOW.isoformat(), "duration": 60}
    first = ingest(
        db,
        archive,
        "activity",
        "862",
        {**base, "activityName": "Original name", "averageHR": 150},
        "UTC",
        fetched_at=NOW,
    )
    last = ingest(
        db,
        archive,
        "activity",
        "862",
        {**base, "duration": 90, "averageHR": "invalid"},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    state = db.get(AppState, "activity-version:862", populate_existing=True)
    state.value = {"requested_at": state.value["requested_at"]}
    db.get(Activity, "862").name = "Old parser name"
    for result in (first, last):
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for result in (last, first) if reverse else (first, last):
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )
    db.expire_all()
    assert db.get(Activity, "862").avg_hr == 150
    assert db.get(Activity, "862").name == "Original name"


def test_replay_started_during_final_model_call_aborts_diary_cited_answer(db, tmp_path):
    import json

    from garmin_ai.agent import AgentStep, ReadCall, answer_question
    from garmin_ai.replay import REPLAY_NOTICE

    archive = LocalArchive(tmp_path)
    result = ingest(
        db, archive, "daily", str(NOW.date()), {"totalSteps": 100}, "UTC", fetched_at=NOW
    )

    class Provider:
        calls = 0

        def structured(self, *args):
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
            db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
            db.flush()
            return AgentStep(answer="Stale Garmin conclusion", evidence_ids=[1])

    assert answer_question(db, Provider(), "synthetic", Settings(), NOW) == REPLAY_NOTICE


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_activity_optional_metadata_retains_source_owner(db, tmp_path, legacy, reverse):
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    base = {"activityId": 871, "startTimeGMT": NOW.isoformat(), "duration": 60}
    first = ingest(
        db,
        archive,
        "activity",
        "871",
        {
            **base,
            "activityName": "Original",
            "activityType": {"typeKey": "running"},
            "timeZoneUnitDTO": {"timeZone": "Europe/Budapest"},
        },
        "UTC",
        fetched_at=NOW,
    )
    last = ingest(
        db,
        archive,
        "activity",
        "871",
        {**base, "duration": 90, "summaryDTO": {"activityName": "Never installed"}},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    state = db.get(AppState, "activity-version:871", populate_existing=True)
    if legacy:
        state.value = {"requested_at": state.value["requested_at"]}
    else:
        assert state.value["owners"]["timezone"] == first["source_ref"]
        assert state.value["owners"]["kind"] == first["source_ref"]
    row = db.get(Activity, "871")
    row.name, row.kind, row.timezone = "Old name", "Old type", "UTC"
    for result in (first, last):
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for result in (last, first) if reverse else (first, last):
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )
    db.expire_all()
    row = db.get(Activity, "871")
    assert (row.name, row.kind, row.timezone) == ("Original", "running", "Europe/Budapest")
    assert row.duration_seconds == 90


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("sleep", [False, True])
def test_newer_retained_projection_replaces_older_owner(db, tmp_path, monkeypatch, reverse, sleep):
    import garmin_ai.normalize as module
    from garmin_ai.models import Measurement, TimelineInterval
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    ts = int(NOW.timestamp() * 1000)
    if sleep:
        endpoint = "sleep"

        def payload(offset, score):
            return {
                "dailySleepDTO": {
                    "sleepStartTimestampGMT": ts + offset,
                    "sleepEndTimestampGMT": ts + offset + 3600000,
                    "sleepScores": {"overall": {"value": score}},
                }
            }

        first_payload, second_payload, last_payload = (
            payload(0, 60),
            payload(60000, 70),
            {"dailySleepDTO": {}},
        )
    else:
        endpoint = "heart_rate"
        first_payload = {"heartRateValues": [[ts, 70]]}
        second_payload = {"heartRateValues": [[ts, 80]], "restingHeartRate": 55}
        last_payload = {"ignored": True}
    first = ingest(db, archive, endpoint, str(NOW.date()), first_payload, "UTC", fetched_at=NOW)
    original_upsert, original_numeric = module.upsert, module.numeric
    if sleep:
        monkeypatch.setattr(
            module,
            "upsert",
            lambda session, model, values, keys: (
                None if model is TimelineInterval else original_upsert(session, model, values, keys)
            ),
        )
    else:
        monkeypatch.setattr(
            module,
            "numeric",
            lambda value, **kw: None if value == 80 else original_numeric(value, **kw),
        )
    second = ingest(
        db,
        archive,
        endpoint,
        str(NOW.date()),
        second_payload,
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    monkeypatch.setattr(module, "upsert", original_upsert)
    monkeypatch.setattr(module, "numeric", original_numeric)
    ingest(
        db,
        archive,
        endpoint,
        str(NOW.date()),
        last_payload,
        "UTC",
        fetched_at=NOW + timedelta(minutes=2),
    )
    for result in (first, second):
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for result in (second, first) if reverse else (first, second):
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )
    db.expire_all()
    if sleep:
        row = db.get(TimelineInterval, f"sleep:{NOW.date()}")
        assert row.start == NOW + timedelta(minutes=1)
        assert row.evidence["source_ref"] == second["source_ref"]
    else:
        assert db.get(Measurement, (NOW, "heart_rate_bpm", "garmin_connect")).value == 80


def test_projection_free_legacy_steps_replays(db, tmp_path):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    result = ingest(db, archive, "steps", str(NOW.date()), [{"steps": 10}], "UTC", fetched_at=NOW)
    db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.delete(db.get(AppState, "ingest-meta:" + result["source_ref"]))
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="UTC"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )


@pytest.mark.parametrize(
    "payload", [{"calendarDate": "2000-01-01", "score": 70}, {"score": "invalid"}]
)
def test_projection_free_legacy_readiness_replays(db, tmp_path, payload):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    result = ingest(db, archive, "readiness", str(NOW.date()), payload, "UTC", fetched_at=NOW)
    db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.delete(db.get(AppState, "ingest-meta:" + result["source_ref"]))
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )


def test_never_applied_legacy_activity_page_uses_configured_timezone(db, tmp_path, monkeypatch):
    import garmin_ai.normalize as module
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    original = module.numeric
    monkeypatch.setattr(module, "numeric", lambda value, **kw: None)
    result = ingest(
        db,
        archive,
        "activities",
        "0",
        [{"activityId": 881, "startTimeGMT": NOW.isoformat(), "duration": 60}],
        "UTC",
        fetched_at=NOW,
    )
    assert result["status"] == "error"
    monkeypatch.setattr(module, "numeric", original)
    db.delete(db.get(AppState, "ingest-meta:" + result["source_ref"]))
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(timezone="Europe/Budapest"),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )


def test_legacy_timezone_uses_latest_observation_application(db, tmp_path):
    from garmin_ai.models import MetricObservation
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    result = ingest(
        db,
        archive,
        "readiness",
        str(NOW.date()),
        {"score": 70, "timestamp": NOW.isoformat()},
        "UTC",
        fetched_at=NOW,
    )
    observation = db.scalar(
        select(MetricObservation).where(MetricObservation.source_ref == UUID(result["source_ref"]))
    )
    db.add(
        MetricObservation(
            **{
                column: getattr(observation, column)
                for column in [
                    "metric",
                    "value",
                    "unit",
                    "source_calendar_date",
                    "source_ref",
                    "observed_at",
                    "effective_start",
                    "account",
                    "device",
                    "quality",
                    "feature_version",
                ]
            },
            timezone="Europe/Budapest",
            sequence=99,
            fetched_at=NOW + timedelta(minutes=1),
            ingested_at=NOW + timedelta(minutes=1),
        )
    )
    db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.delete(db.get(AppState, "ingest-meta:" + result["source_ref"]))
    db.flush()
    replay_source(
        db,
        archive,
        Settings(timezone="UTC"),
        {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
    )
    assert (
        db.get(AppState, "ingest-meta:" + result["source_ref"], populate_existing=True).value[
            "timezone"
        ]
        == "Europe/Budapest"
    )


@pytest.mark.parametrize("fails", [False, True])
def test_retained_fit_replays_after_empty_fetch(db, tmp_path, monkeypatch, fails):
    import garmin_ai.fit as fit
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    db.add(
        Activity(
            id="synthetic-empty",
            start=NOW,
            end=NOW + timedelta(minutes=1),
            kind="running",
            timezone="UTC",
        )
    )
    db.flush()
    monkeypatch.setattr(fit, "extract_fit", lambda data: [data])
    monkeypatch.setattr(fit, "parse_fit", lambda data: [("record", {"heart_rate": 70})])
    first = fit.store_fit(db, archive, "synthetic-empty", b"synthetic", NOW)
    empty = fit.store_fit(db, archive, "synthetic-empty", b"", NOW + timedelta(minutes=1))
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    if fails:

        def fail(data):
            raise ValueError("synthetic failure")

        monkeypatch.setattr(fit, "parse_fit", fail)
    result = replay_source(
        db, archive, Settings(), {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION}
    )
    assert result["status"] == ("error" if fails else "normalized")
    state = db.get(AppState, "fit-version:synthetic-empty", populate_existing=True).value
    assert state["source_ref"] == empty["source_ref"]
    assert "latest_attempt" not in state


def test_rejected_retained_daily_field_keeps_future_replay_owner(db, tmp_path, monkeypatch):
    import garmin_ai.normalize as module
    from garmin_ai.replay import canonical_source, replay_source

    archive = LocalArchive(tmp_path)
    first = ingest(
        db, archive, "daily", str(NOW.date()), {"restingHeartRate": 60}, "UTC", fetched_at=NOW
    )
    ingest(
        db,
        archive,
        "daily",
        str(NOW.date()),
        {"totalSteps": 100},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value == 60 else original(value, **kw)
    )
    row = db.get(SourcePayload, UUID(first["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    request = {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION}
    replay_source(db, archive, Settings(), request)
    db.expire_all()
    assert db.get(HealthDay, NOW.date()).resting_hr is None
    assert (
        db.scalar(select(SourcePayload.id).where(SourcePayload.id == row.id, canonical_source()))
        == row.id
    )
    monkeypatch.setattr(module, "numeric", original)
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    replay_source(db, archive, Settings(), request)
    db.expire_all()
    assert db.get(HealthDay, NOW.date()).resting_hr == 60


@pytest.mark.parametrize("status", ["superseded_revision", "source_removed", "unchanged"])
def test_noop_replay_does_not_advance_generation(db, tmp_path, monkeypatch, status):
    import garmin_ai.replay as module

    archive = LocalArchive(tmp_path)
    result = ingest(
        db, archive, "daily", str(NOW.date()), {"totalSteps": 100}, "UTC", fetched_at=NOW
    )
    before = module.replay_generation(db)
    monkeypatch.setattr(module, "ingest", lambda *args, **kwargs: {"status": status})
    assert (
        module.replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == status
    )
    assert module.replay_generation(db) == before


@pytest.mark.parametrize("replace_later", [False, True])
def test_rejected_retained_sample_keeps_durable_owner(db, tmp_path, monkeypatch, replace_later):
    import garmin_ai.normalize as module
    from garmin_ai.models import Measurement
    from garmin_ai.replay import canonical_source, replay_source

    archive = LocalArchive(tmp_path)
    stamp = int(NOW.timestamp() * 1000)
    first = ingest(
        db,
        archive,
        "heart_rate",
        str(NOW.date()),
        {"heartRateValues": [[stamp, 70]]},
        "UTC",
        fetched_at=NOW,
    )
    ingest(
        db,
        archive,
        "heart_rate",
        str(NOW.date()),
        {"restingHeartRate": 60},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value == 70 else original(value, **kw)
    )
    row = db.get(SourcePayload, UUID(first["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    request = {"raw_ref": str(row.id), "target_version": PARSER_VERSION}
    replay_source(db, archive, Settings(), request)
    assert (
        db.scalar(select(SourcePayload.id).where(SourcePayload.id == row.id, canonical_source()))
        == row.id
    )
    assert db.scalar(select(Measurement).where(Measurement.source_ref == row.id)) is None
    if replace_later:
        from garmin_ai.reconciliation import Replacement

        ingest(
            db,
            archive,
            "heart_rate",
            str(NOW.date()),
            {"heartRateValues": [[stamp + 60000, 80]]},
            "UTC",
            fetched_at=NOW + timedelta(minutes=2),
            replacement=Replacement(
                NOW, NOW + timedelta(hours=1), ("heart_rate_bpm",), "synthetic"
            ),
        )
    monkeypatch.setattr(module, "numeric", original)
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    result = replay_source(db, archive, Settings(), request)
    assert (result["status"] == "superseded_revision") == replace_later
    assert (db.scalar(select(Measurement).where(Measurement.source_ref == row.id)) is not None) == (
        not replace_later
    )


def test_live_parser_transition_invalidates_outputs(db, tmp_path):
    from garmin_ai.models import PendingQuestion
    from garmin_ai.replay import replay_generation

    archive = LocalArchive(tmp_path)
    payload = {"totalSteps": 100}
    result = ingest(db, archive, "daily", str(NOW.date()), payload, "UTC", fetched_at=NOW)
    row = db.get(SourcePayload, UUID(result["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    question = PendingQuestion(
        kind="context",
        text="synthetic",
        evidence={},
        priority=1,
        earliest_send_at=NOW,
        expires_at=NOW + timedelta(days=1),
        status="pending",
        dedup_key="live-upgrade",
    )
    insight = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="accepted",
        dedup_key="live-upgrade",
    )
    db.add_all([question, insight])
    db.flush()
    before = replay_generation(db)
    ingest(
        db, archive, "daily", str(NOW.date()), payload, "UTC", fetched_at=NOW + timedelta(minutes=1)
    )
    assert replay_generation(db) != before
    db.refresh(question)
    db.refresh(insight)
    assert question.status == "cancelled"
    assert insight.status == "superseded"


@pytest.mark.parametrize("reverse", [False, True])
def test_retained_sample_respects_rejected_newer_owner(db, tmp_path, monkeypatch, reverse):
    import garmin_ai.normalize as module
    from garmin_ai.models import Measurement
    from garmin_ai.reconciliation import Replacement
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    stamp = int(NOW.timestamp() * 1000)
    first = ingest(
        db,
        archive,
        "heart_rate",
        str(NOW.date()),
        {"restingHeartRate": 60, "heartRateValues": [[stamp, 70]]},
        "UTC",
        fetched_at=NOW,
    )
    second = ingest(
        db,
        archive,
        "heart_rate",
        str(NOW.date()),
        {"heartRateValues": [[stamp, 80]]},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
        replacement=Replacement(NOW, NOW + timedelta(hours=1), ("heart_rate_bpm",), "synthetic"),
    )
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value == 80 else original(value, **kw)
    )
    for item in [first, second]:
        db.get(SourcePayload, UUID(item["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for item in [second, first] if reverse else [first, second]:
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": item["source_ref"], "target_version": PARSER_VERSION},
        )
    assert db.scalar(select(Measurement).where(Measurement.ts == NOW)) is None
    # Authoritative replacements remain in the journal even without a projection.
    monkeypatch.setattr(module, "numeric", original)
    db.get(SourcePayload, UUID(second["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    replay_source(
        db, archive, Settings(), {"raw_ref": second["source_ref"], "target_version": PARSER_VERSION}
    )
    db.expire_all()
    assert db.scalar(select(Measurement).where(Measurement.ts == NOW)).value == 80


def test_rejected_sleep_interval_retains_owner(db, tmp_path, monkeypatch):
    import garmin_ai.ingest as module
    from garmin_ai.models import TimelineInterval
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "sleep",
        str(NOW.date()),
        {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": int(NOW.timestamp() * 1000),
                "sleepEndTimestampGMT": int((NOW + timedelta(hours=1)).timestamp() * 1000),
            }
        },
        "UTC",
        fetched_at=NOW,
    )
    ingest(
        db,
        archive,
        "sleep",
        str(NOW.date()),
        {"dailySleepDTO": {"sleepTimeSeconds": 3600}},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    original = module.normalize
    monkeypatch.setattr(
        module,
        "normalize",
        lambda session, endpoint, key, payload, ref, zone: original(
            session, endpoint, key, {}, ref, zone
        ),
    )
    row = db.get(SourcePayload, UUID(first["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    request = {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION}
    replay_source(db, archive, Settings(), request)
    assert db.scalar(select(TimelineInterval)) is None
    monkeypatch.setattr(module, "normalize", original)
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    assert replay_source(db, archive, Settings(), request)["status"] == "normalized"
    assert db.scalar(select(TimelineInterval)) is not None


def test_legacy_activity_tied_timestamp_uses_installed_value(db, tmp_path):
    from garmin_ai.normalize import legacy_activity_owners

    archive = LocalArchive(tmp_path)
    base = {"activityId": 999, "startTimeGMT": NOW.isoformat(), "duration": 60}
    ingest(db, archive, "activity", "999", {**base, "averageHR": 120}, "UTC", fetched_at=NOW)
    last = ingest(db, archive, "activity", "999", {**base, "averageHR": 150}, "UTC", fetched_at=NOW)
    assert legacy_activity_owners(db, "999")["avg_hr"] == last["source_ref"]


@pytest.mark.parametrize("start_pending", [False, True])
def test_diary_evidence_survives_replay_with_unique_ids(db, tmp_path, monkeypatch, start_pending):
    import json

    import garmin_ai.agent as module
    from garmin_ai.agent import AgentStep, ReadCall, answer_question
    from garmin_ai.replay import invalidate_outputs

    row = raw(db, LocalArchive(tmp_path), NOW)
    row.parser_version = PARSER_VERSION - 1 if start_pending else PARSER_VERSION
    db.flush()
    monkeypatch.setattr(module, "call_tool", lambda *args: {"items": []})

    class Provider:
        count = 0

        def structured(self, instruction, prompt, schema):
            self.count += 1
            if self.count == 1:
                calls = [ReadCall(name="events", arguments_json="{}")]
                if not start_pending:
                    calls.insert(0, ReadCall(name="health_range", arguments_json="{}"))
                return AgentStep(calls=calls)
            if self.count == 2:
                row.parser_version = PARSER_VERSION - 1
                db.flush()
                if start_pending:
                    invalidate_outputs(db)
                return AgentStep(calls=[ReadCall(name="events", arguments_json="{}")])
            ids = [item["id"] for item in json.loads(prompt)["evidence"]]
            assert len(ids) == len(set(ids))
            if not start_pending:
                assert ids == [2, 3]
            return AgentStep(answer="Diary checked", evidence_ids=ids)

    assert "Diary checked" in answer_question(db, Provider(), "synthetic", Settings(), NOW)


@pytest.mark.parametrize("reverse", [False, True])
def test_retained_daily_equal_timestamp_preserves_field_owner(db, tmp_path, reverse):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "daily",
        str(NOW.date()),
        {"totalSteps": 100, "restingHeartRate": 60},
        "UTC",
        fetched_at=NOW,
    )
    second = ingest(
        db, archive, "daily", str(NOW.date()), {"totalSteps": 200}, "UTC", fetched_at=NOW
    )
    for item in (first, second):
        db.get(SourcePayload, UUID(item["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for item in [second, first] if reverse else [first, second]:
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": item["source_ref"], "target_version": PARSER_VERSION},
        )
    db.expire_all()
    day = db.get(HealthDay, NOW.date())
    assert day.steps == 200 and day.resting_hr == 60
    assert day.sources["field:steps"] == second["source_ref"]


def test_live_fit_holds_normalization_fence(db, db_engine, tmp_path):
    from sqlalchemy import text

    from garmin_ai.fit import store_fit

    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "activity",
        "998",
        {"activityId": 998, "startTimeGMT": NOW.isoformat(), "duration": 60},
        "UTC",
        fetched_at=NOW,
    )
    db.commit()
    assert store_fit(db, archive, "998", b"", NOW)["status"] == "empty"
    with db_engine.begin() as probe:
        assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104619)"))


@pytest.mark.parametrize("stale,partial", [(False, False), (True, False), (True, True)])
def test_analysis_delivery_checks_generation_under_fence(db, db_engine, stale, partial):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import text

    from garmin_ai.conversation import conversation_context
    from garmin_ai.replay import REPLAY_NOTICE, invalidate_outputs, replay_generation
    from garmin_ai.telegram import deliver

    epoch = conversation_context(db, NOW, None)["epoch"]
    db.add(
        AppState(
            key="telegram:reply:991",
            value={
                "kind": "analysis",
                "analysis_epoch": epoch,
                "analysis_projection": {"generation": replay_generation(db)},
                "text": "synthetic",
            },
        )
    )
    db.commit()
    if partial:
        db.add(AppState(key="outbox:update:991:0", value={"status": "sent", "formatted": True}))
        db.commit()
    if stale:
        invalidate_outputs(db)
        db.commit()
    sent = []

    class Bot:
        async def send_message(self, **kw):
            with db_engine.begin() as probe:
                assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104619)"))
            sent.append(kw["text"])
            return SimpleNamespace(message_id=991)

    asyncio.run(deliver(Bot(), db_engine, 42, "update:991", "synthetic"))
    assert sent == [REPLAY_NOTICE if stale else "synthetic"]


def test_health_tool_holds_read_fence(db, db_engine, monkeypatch):
    from sqlalchemy import text

    import garmin_ai.tools as module

    tool = module.TOOLS["health_range"]

    def read(session, **kw):
        with db_engine.begin() as probe:
            assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104619)"))
        return {}

    monkeypatch.setattr(tool, "fn", read)
    module.call_tool(db, "health_range", {"start": str(NOW.date()), "end": str(NOW.date())})


@pytest.mark.parametrize("tied", [False, True])
def test_retained_activity_name_uses_installed_timing(db, tmp_path, monkeypatch, tied):
    import garmin_ai.normalize as module
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    base = {"activityId": 997, "startTimeGMT": NOW.isoformat()}
    first = ingest(
        db,
        archive,
        "activity",
        "997",
        {**base, "duration": 60, "activityName": "synthetic"},
        "UTC",
        fetched_at=NOW,
    )
    ingest(
        db,
        archive,
        "activity",
        "997",
        {**base, "duration": 90},
        "UTC",
        fetched_at=NOW if tied else NOW + timedelta(minutes=1),
    )
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value == 60 else original(value, **kw)
    )
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    db.expire_all()
    assert db.get(Activity, "997").end == NOW + timedelta(seconds=90)
    assert db.get(Activity, "997").duration_seconds == 90
    assert db.get(Activity, "997").name == "synthetic"


def test_replay_does_not_replace_urgent_safety_reply(db):
    from garmin_ai.agent import AgentStep, answer_question
    from garmin_ai.replay import invalidate_outputs

    class Provider:
        def structured(self, *args):
            invalidate_outputs(db)
            return AgentStep(urgent_safety=True)

    response = answer_question(db, Provider(), "synthetic", Settings(), NOW)
    assert "112" in response
    assert db.info.get("analysis_projection") is None


def test_replay_retires_conversation_and_prevents_stale_pending_promotion(db):
    from garmin_ai.conversation import KEY, PENDING_KEY, conversation_context, remember_answer
    from garmin_ai.replay import invalidate_outputs, replay_generation

    remember_answer(db, NOW, 881, "synthetic", "old", [], epoch=None)
    db.add(AppState(key="outbox:update:881:0", value={"status": "sent"}))
    db.flush()
    assert conversation_context(db, NOW)["turns"]
    old_generation = replay_generation(db)
    remember_answer(db, NOW, 882, "synthetic", "unseen", [], epoch=None)
    invalidate_outputs(db)
    db.add(AppState(key="outbox:update:882:0", value={"status": "sent"}))
    db.flush()
    assert conversation_context(db, NOW)["turns"] == []
    assert db.get(AppState, PENDING_KEY) is None
    db.info["analysis_projection"] = {"generation": old_generation}
    remember_answer(db, NOW, 883, "synthetic", "late stale", [], epoch=None)
    assert db.get(AppState, PENDING_KEY) is None
    assert db.get(AppState, KEY).value["turns"] == []


@pytest.mark.parametrize("reverse", [False, True])
def test_temporal_replay_preserves_equal_fetch_precedence(db, tmp_path, reverse):
    from garmin_ai.replay import replay_source
    from garmin_ai.temporal import feature_at

    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "readiness",
        str(NOW.date()),
        {"timestamp": NOW.isoformat(), "score": 60},
        "UTC",
        fetched_at=NOW,
    )
    second = ingest(
        db,
        archive,
        "readiness",
        str(NOW.date()),
        {"timestamp": NOW.isoformat(), "score": 80},
        "UTC",
        fetched_at=NOW,
    )
    times = {str(row.source_ref): row.ingested_at for row in db.scalars(select(MetricObservation))}
    for item in (first, second):
        db.get(SourcePayload, UUID(item["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for item in [second, first] if reverse else [first, second]:
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": item["source_ref"], "target_version": PARSER_VERSION},
        )
    db.expire_all()
    observations = db.scalars(select(MetricObservation)).all()
    assert {str(row.source_ref): row.ingested_at for row in observations} == times
    metric = observations[0].metric
    assert feature_at(db, metric, NOW + timedelta(hours=1), datetime.now(UTC))["value"] == 80


def test_rejected_temporal_observation_remains_replayable(db, tmp_path, monkeypatch):
    import garmin_ai.normalize as module
    from garmin_ai.replay import canonical_source, replay_source

    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "readiness",
        str(NOW.date()),
        {"timestamp": NOW.isoformat(), "score": 60},
        "UTC",
        fetched_at=NOW,
    )
    ingest(
        db,
        archive,
        "readiness",
        str(NOW.date()),
        {"timestamp": NOW.isoformat(), "score": 80},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    old_time = db.scalar(
        select(MetricObservation.ingested_at).where(
            MetricObservation.source_ref == UUID(first["source_ref"])
        )
    )
    original = module.numeric
    monkeypatch.setattr(
        module, "numeric", lambda value, **kw: None if value == 60 else original(value, **kw)
    )
    row = db.get(SourcePayload, UUID(first["source_ref"]))
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    request = {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION}
    replay_source(db, archive, Settings(), request)
    assert (
        db.scalar(select(MetricObservation).where(MetricObservation.source_ref == row.id)) is None
    )
    assert (
        db.scalar(select(SourcePayload.id).where(SourcePayload.id == row.id, canonical_source()))
        == row.id
    )
    monkeypatch.setattr(module, "numeric", original)
    row.parser_version = PARSER_VERSION - 1
    db.flush()
    assert replay_source(db, archive, Settings(), request)["status"] == "normalized"
    assert (
        db.scalar(
            select(MetricObservation.ingested_at).where(MetricObservation.source_ref == row.id)
        )
        == old_time
    )


def test_reused_temporal_raw_replays_every_application(db, tmp_path):
    from garmin_ai.replay import replay_source
    from garmin_ai.temporal import feature_at

    archive = LocalArchive(tmp_path)
    payload = {"timestamp": NOW.isoformat(), "score": 60}
    first = ingest(db, archive, "readiness", str(NOW.date()), payload, "UTC", fetched_at=NOW)
    ingest(
        db,
        archive,
        "readiness",
        str(NOW.date()),
        {**payload, "score": 80},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    ingest(
        db,
        archive,
        "readiness",
        str(NOW.date()),
        payload,
        "UTC",
        fetched_at=NOW + timedelta(minutes=2),
    )
    for obs in db.scalars(select(MetricObservation)):
        obs.ingested_at = obs.fetched_at
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    replay_source(
        db, archive, Settings(), {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION}
    )
    db.expire_all()
    rows = db.scalars(
        select(MetricObservation).where(MetricObservation.source_ref == UUID(first["source_ref"]))
    ).all()
    assert {row.fetched_at for row in rows} == {NOW, NOW + timedelta(minutes=2)}
    assert (
        feature_at(
            db, rows[0].metric, NOW + timedelta(seconds=45), NOW + timedelta(seconds=30), "as_known"
        )["value"]
        == 60
    )


@pytest.mark.parametrize("endpoint", ["activity_details", "activity_splits", "activity_weather"])
def test_legacy_empty_activity_parts_keep_owner(db, tmp_path, endpoint):
    from garmin_ai.models import ActivityPart
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    ingest(
        db,
        archive,
        "activity",
        "996",
        {"activityId": 996, "startTimeGMT": NOW.isoformat(), "duration": 60},
        "UTC",
        fetched_at=NOW,
    )
    first = ingest(db, archive, endpoint, "996", {"synthetic": 1}, "UTC", fetched_at=NOW)
    empty = ingest(db, archive, endpoint, "996", {}, "UTC", fetched_at=NOW + timedelta(minutes=1))
    db.delete(db.get(AppState, f"activity-parts-owner:996:{endpoint}"))
    state = db.get(AppState, f"ingest:garmin_connect:{endpoint}:996")
    state.value = {
        "source_ref": empty["source_ref"],
        "status": "empty",
        "requested_at": (NOW + timedelta(minutes=1)).isoformat(),
    }
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "archived"
    )
    assert (
        db.get(AppState, f"activity-parts-owner:996:{endpoint}", populate_existing=True).value[
            "source_ref"
        ]
        == first["source_ref"]
    )
    assert db.scalar(select(ActivityPart).where(ActivityPart.kind == endpoint)).payload == {
        "synthetic": 1
    }


@pytest.mark.parametrize("safety", [False, True])
@pytest.mark.parametrize("replay_state", ["none", "pending", "completed"])
def test_legacy_analysis_delivery_is_fenced(db, db_engine, tmp_path, safety, replay_state):
    import asyncio
    from types import SimpleNamespace

    from garmin_ai.conversation import conversation_context
    from garmin_ai.replay import REPLAY_NOTICE, invalidate_outputs
    from garmin_ai.telegram import deliver

    value = {
        "kind": "analysis",
        "analysis_epoch": conversation_context(db, NOW, None)["epoch"],
        "text": "synthetic",
    }
    if safety:
        value["analysis_projection"] = None
    db.add(AppState(key="telegram:reply:992", value=value))
    if replay_state == "pending":
        raw(db, LocalArchive(tmp_path), NOW)
    elif replay_state == "completed":
        invalidate_outputs(db)
    db.commit()
    sent = []

    class Bot:
        async def send_message(self, **kw):
            sent.append(kw["text"])
            return SimpleNamespace(message_id=992)

    asyncio.run(deliver(Bot(), db_engine, 42, "update:992", "synthetic"))
    assert sent == [REPLAY_NOTICE if not safety and replay_state != "none" else "synthetic"]


def test_today_snapshot_fences_read_and_delivery(db, db_engine):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import event, text

    from garmin_ai.replay import REPLAY_NOTICE, invalidate_outputs
    from garmin_ai.telegram import deliver, process_message, save_update

    db.add(HealthDay(day=NOW.date(), training_readiness_score=70))
    save_update(
        db,
        {
            "update_id": 993,
            "message": {
                "message_id": 993,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/today",
            },
        },
        42,
    )
    db.commit()
    reads = []

    def check_lock(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT health_days."):
            with db_engine.begin() as probe:
                assert not probe.scalar(text("SELECT pg_try_advisory_xact_lock(72104619)"))
            reads.append(True)

    event.listen(db_engine, "before_cursor_execute", check_lock)
    try:
        response = process_message(db_engine, None, Settings(telegram_user_id=42), 993)
    finally:
        event.remove(db_engine, "before_cursor_execute", check_lock)
    assert reads and "70" in response
    invalidate_outputs(db)
    db.commit()
    sent = []

    class Bot:
        async def send_message(self, **kw):
            sent.append(kw["text"])
            return SimpleNamespace(message_id=993)

    asyncio.run(deliver(Bot(), db_engine, 42, "update:993", response))
    assert sent == [REPLAY_NOTICE]


def test_new_temporal_metric_restores_prior_raw_applications(db, tmp_path, monkeypatch):
    import garmin_ai.normalize as module
    from garmin_ai.replay import replay_source
    from garmin_ai.temporal import feature_at

    archive = LocalArchive(tmp_path)
    original = module.numeric
    with monkeypatch.context() as patch:
        patch.setattr(module, "numeric", lambda v, **kw: None if v == 123 else original(v, **kw))
        payload = {"timestamp": NOW.isoformat(), "score": 60, "recoveryTime": 123}
        first = ingest(db, archive, "readiness", str(NOW.date()), payload, "UTC", fetched_at=NOW)
        ingest(
            db,
            archive,
            "readiness",
            str(NOW.date()),
            {**payload, "score": 80},
            "UTC",
            fetched_at=NOW + timedelta(minutes=1),
        )
        ingest(
            db,
            archive,
            "readiness",
            str(NOW.date()),
            payload,
            "UTC",
            fetched_at=NOW + timedelta(minutes=2),
        )
    for obs in db.scalars(select(MetricObservation)):
        obs.ingested_at = obs.fetched_at
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    replay_source(
        db, archive, Settings(), {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION}
    )
    db.expire_all()
    assert (
        feature_at(
            db,
            "recovery_time_minutes",
            NOW + timedelta(seconds=45),
            NOW + timedelta(seconds=30),
            "as_known",
        )["value"]
        == 123
    )
    rows = db.scalars(
        select(MetricObservation).where(
            MetricObservation.source_ref == UUID(first["source_ref"]),
            MetricObservation.metric == "recovery_time_minutes",
        )
    ).all()
    assert {row.fetched_at for row in rows} == {NOW, NOW + timedelta(minutes=2)}


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_tied_activity_replay_preserves_installed_timing(db, tmp_path, reverse):
    from garmin_ai.models import Activity
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "activity",
        "995",
        {
            "activityId": 995,
            "startTimeGMT": NOW.isoformat(),
            "duration": 60,
            "activityName": "synthetic",
        },
        "UTC",
        fetched_at=NOW,
    )
    later_start = NOW + timedelta(hours=1)
    second = ingest(
        db,
        archive,
        "activity",
        "995",
        {
            "activityId": 995,
            "startTimeGMT": later_start.isoformat(),
            "duration": 90,
            "elapsedDuration": 120,
        },
        "UTC",
        fetched_at=NOW,
    )
    state = db.get(AppState, "activity-version:995")
    state.value = {"requested_at": NOW.isoformat()}
    for result in (first, second):
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    for result in (second, first) if reverse else (first, second):
        assert (
            replay_source(
                db,
                archive,
                Settings(),
                {"raw_ref": result["source_ref"], "target_version": PARSER_VERSION},
            )["status"]
            == "normalized"
        )
        db.expire_all()
        activity = db.get(Activity, "995")
        assert (activity.start, activity.end) == (later_start, later_start + timedelta(seconds=120))
    assert db.get(Activity, "995").name == "synthetic"
    owners = db.get(AppState, "activity-version:995").value["owners"]
    assert owners["start"] == owners["end"] == second["source_ref"]


def test_legacy_failed_activity_cannot_claim_uninstalled_fields(db, tmp_path, monkeypatch):
    import importlib

    from garmin_ai.replay import replay_source

    module = importlib.import_module("garmin_ai.ingest")
    archive = LocalArchive(tmp_path)
    first = ingest(
        db,
        archive,
        "activity",
        "994",
        {"activityId": 994, "startTimeGMT": NOW.isoformat(), "duration": 60, "averageHR": 120},
        "UTC",
        fetched_at=NOW,
    )
    original = module.normalize

    def fail(*args):
        raise ValueError("synthetic failure")

    monkeypatch.setattr(module, "normalize", fail)
    failed = ingest(
        db,
        archive,
        "activity",
        "994",
        {"activityId": 994, "startTimeGMT": NOW.isoformat(), "duration": 90, "averageHR": 150},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    for result in (first, failed):
        db.get(SourcePayload, UUID(result["source_ref"])).parser_version = PARSER_VERSION - 1
        db.delete(db.get(AppState, "ingest-meta:" + result["source_ref"]))
    db.get(AppState, "activity-version:994").value = {"requested_at": NOW.isoformat()}
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": failed["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "error"
    )
    monkeypatch.setattr(module, "normalize", original)
    assert (
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    owners = db.get(AppState, "activity-version:994", populate_existing=True).value["owners"]
    assert owners["duration_seconds"] == owners["avg_hr"] == first["source_ref"]
    assert replay_status(db)["ready"]


def test_diary_analysis_continues_when_pending_replay_completes(db, tmp_path, monkeypatch):
    import garmin_ai.agent as module
    from garmin_ai.agent import AgentStep, ReadCall
    from garmin_ai.replay import invalidate_outputs, replay_generation

    row = raw(db, LocalArchive(tmp_path), NOW)
    monkeypatch.setattr(module, "call_tool", lambda *args: {"items": []})

    class Provider:
        calls = 0

        def structured(self, *args):
            self.calls += 1
            if self.calls == 1:
                row.parser_version = PARSER_VERSION
                invalidate_outputs(db)
                return AgentStep(calls=[ReadCall(name="events", arguments_json="{}")])
            return AgentStep(answer="Synthetic diary answer", evidence_ids=[1])

    assert "Synthetic diary answer" in module.answer_question(
        db, Provider(), "synthetic", Settings(), NOW
    )
    assert db.info["analysis_projection"] == {"generation": replay_generation(db)}


def test_legacy_reused_daily_raw_keeps_installed_application_clock(db, tmp_path):
    from garmin_ai.replay import replay_source

    archive = LocalArchive(tmp_path)
    payload = {"totalSteps": 100, "restingHeartRate": 50}
    first = ingest(db, archive, "daily", str(NOW.date()), payload, "UTC", fetched_at=NOW)
    ingest(
        db,
        archive,
        "daily",
        str(NOW.date()),
        {"totalSteps": 200, "restingHeartRate": 60},
        "UTC",
        fetched_at=NOW + timedelta(minutes=1),
    )
    ingest(
        db, archive, "daily", str(NOW.date()), payload, "UTC", fetched_at=NOW + timedelta(minutes=2)
    )
    ingest(
        db,
        archive,
        "daily",
        str(NOW.date()),
        {"totalSteps": 300},
        "UTC",
        fetched_at=NOW + timedelta(minutes=3),
    )
    db.delete(db.get(AppState, "ingest-meta:" + first["source_ref"]))
    db.get(SourcePayload, UUID(first["source_ref"])).parser_version = PARSER_VERSION - 1
    db.flush()
    assert (
        replay_source(
            db,
            archive,
            Settings(),
            {"raw_ref": first["source_ref"], "target_version": PARSER_VERSION},
        )["status"]
        == "normalized"
    )
    day = db.get(HealthDay, NOW.date(), populate_existing=True)
    assert day.steps == 300 and day.resting_hr == 50
    assert datetime.fromisoformat(day.sources["time:resting_hr"]) == NOW + timedelta(minutes=2)


@pytest.mark.parametrize("fit", [False, True])
def test_live_promotion_of_failed_parser_zero_invalidates_outputs(db, tmp_path, monkeypatch, fit):
    import importlib

    from garmin_ai.replay import replay_generation

    archive = LocalArchive(tmp_path)
    module = importlib.import_module("garmin_ai.fit" if fit else "garmin_ai.ingest")
    if fit:
        ingest(
            db,
            archive,
            "activity",
            "993",
            {"activityId": 993, "startTimeGMT": NOW.isoformat(), "duration": 60},
            "UTC",
            fetched_at=NOW,
        )
        monkeypatch.setattr(module, "extract_fit", lambda data: [data])
        monkeypatch.setattr(module, "parse_fit", lambda data: [("record", {"heart_rate": 70})])
        module.store_fit(db, archive, "993", b"old", NOW)
        original = module.parse_fit
        attribute = "parse_fit"

        def apply(at):
            return module.store_fit(db, archive, "993", b"new", at)
    else:
        ingest(db, archive, "daily", str(NOW.date()), {"totalSteps": 100}, "UTC", fetched_at=NOW)
        original = module.normalize
        attribute = "normalize"

        def apply(at):
            return ingest(
                db, archive, "daily", str(NOW.date()), {"totalSteps": 200}, "UTC", fetched_at=at
            )

    def fail(*args):
        raise ValueError("synthetic old parser failure")

    monkeypatch.setattr(module, attribute, fail)
    failed = apply(NOW + timedelta(minutes=1))
    assert failed["status"] == "error"
    assert db.get(SourcePayload, UUID(failed["source_ref"])).parser_version == 0
    db.get(AppState, "ingest-meta:" + failed["source_ref"]).value = {
        "failed_parser_version": PARSER_VERSION - 1
    }
    insight = Insight(
        category="synthetic",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="accepted",
        dedup_key="failed-promotion",
    )
    db.add(insight)
    db.flush()
    before = replay_generation(db)
    monkeypatch.setattr(module, attribute, original)
    assert apply(NOW + timedelta(minutes=2))["status"] == "normalized"
    assert replay_generation(db) != before
    db.refresh(insight)
    assert insight.status == "superseded"
