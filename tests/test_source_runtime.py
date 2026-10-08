"""Exercise the source worker against private synthetic data and a disposable DB."""

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.models import AppState, Job, SourcePayload
from garmin_ai.source_runtime import configured_source_plugins, poll_source_instance


@pytest.fixture(autouse=True)
def installed_fixture():
    pytest.importorskip("synthetic_adapters")


def selected_settings():
    return Settings(
        integrations=[
            IntegrationInstance(
                id="source:sample:one", kind="source", provider="sample", config={"label": "one"}
            )
        ]
    )


NOW = datetime(2026, 1, 2, tzinfo=UTC)


def test_source_worker_persists_pages_and_cursor_together(db, db_engine):
    settings = selected_settings()
    assert configured_source_plugins(settings) == ("source:sample:one",)
    assert poll_source_instance(db_engine, settings, "source:sample:one", now=NOW, max_pages=1) == {
        "status": "partial",
        "records": 2,
        "pages": 1,
    }
    db.expire_all()
    cursor = db.get(AppState, "source-plugin:cursor:source:sample:one")
    assert cursor.value["next_cursor"] == "2"
    assert len(db.scalars(select(SourcePayload)).all()) == 2

    assert poll_source_instance(db_engine, settings, "source:sample:one", now=NOW) == {
        "status": "complete",
        "records": 1,
        "pages": 1,
    }
    db.expire_all()
    rows = db.scalars(select(SourcePayload)).all()
    assert {row.source_key for row in rows} == {"walk-1", "walk-2", "walk-3"}
    assert {row.source for row in rows} == {"plugin:source:sample:one"}
    assert {row.status for row in rows} == {"raw_only"}
    assert all(row.archive_key.startswith("inline:") for row in rows)
    assert db.get(AppState, "source-plugin:cursor:source:sample:one").value["next_cursor"] is None

    assert poll_source_instance(db_engine, settings, "source:sample:one", now=NOW)["records"] == 3
    db.expire_all()
    assert len(db.scalars(select(SourcePayload)).all()) == 3


def test_source_worker_detects_cursor_cycle_across_bounded_jobs(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    from garmin_ai.source_contracts import SourcePage

    def cyclic_page(self, *, start, end, cursor, limit):
        step = int(cursor or "0")
        return SourcePage(
            instance_id=self.instance_id,
            page_kind="partial",
            fetched_at=NOW,
            next_cursor=str(step + 1) if step < 11 else "1",
        )

    monkeypatch.setattr(SampleSource, "read_page", cyclic_page)
    settings = selected_settings()
    assert (
        poll_source_instance(db_engine, settings, "source:sample:one", now=NOW, max_pages=10)[
            "status"
        ]
        == "partial"
    )
    db.expire_all()
    state = db.get(AppState, "source-plugin:cursor:source:sample:one")
    assert state.value["next_cursor"] == "10"
    assert len(state.value["seen_cursor_hashes"]) == 10

    with pytest.raises(ValueError, match="repeated a cursor"):
        poll_source_instance(db_engine, settings, "source:sample:one", now=NOW, max_pages=2)
    db.expire_all()
    assert db.get(AppState, "source-plugin:cursor:source:sample:one").value["next_cursor"] == ("11")


def test_source_worker_respects_onboarding_selection(db, db_engine):
    db.add(AppState(key="preferences:onboarding", value={"source_instance_ids": []}))
    db.commit()
    result = poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW)
    assert result["status"] == "disabled"
    db.expire_all()
    assert db.scalars(select(SourcePayload)).all() == []


def test_source_worker_revalidates_capabilities_and_page_type(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    from garmin_ai.source_contracts import SourceCapabilities

    with monkeypatch.context() as patcher:
        patcher.setattr(
            SampleSource,
            "capabilities",
            property(
                lambda _self: SourceCapabilities.model_construct(time_semantics="unsupported")
            ),
        )
        with pytest.raises(ValidationError, match="time_semantics"):
            poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW)

    original = SampleSource.read_page

    def wrong_page_type(self, *, start, end, cursor, limit):
        return original(self, start=start, end=end, cursor=cursor, limit=limit).model_dump(
            mode="python"
        )

    monkeypatch.setattr(SampleSource, "read_page", wrong_page_type)
    with pytest.raises(ValueError, match="invalid page type"):
        poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW)
    db.expire_all()
    assert db.scalars(select(SourcePayload)).all() == []


def test_non_garmin_payloads_do_not_enter_garmin_replay(db, db_engine):
    from garmin_ai.normalize import PARSER_VERSION
    from garmin_ai.replay import replay_pending_condition, replay_source, schedule_replay

    poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW)
    db.add(
        SourcePayload(
            source="file_import:synthetic:device",
            endpoint="user.synthetic",
            source_key="fictional-row",
            payload_hash="synthetic-digest",
            payload={"fictional": True},
            archive_key="sha256:synthetic",
            fetched_at=NOW,
            parser_version=0,
            status="applied",
        )
    )
    db.add(AppState(key="account:garmin", value={"fingerprint": "synthetic"}))
    db.commit()

    assert not db.scalar(select(replay_pending_condition()))
    assert schedule_replay(db, NOW) == 0
    assert db.scalars(select(Job).where(Job.kind == "raw_replay")).all() == []
    for row in db.scalars(select(SourcePayload)):
        assert replay_source(
            db,
            object(),
            None,
            {"raw_ref": str(row.id), "target_version": PARSER_VERSION},
        ) == {"status": "unsupported_source"}


def test_source_worker_accepts_interval_overlapping_window_start(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    from garmin_ai.source_contracts import SourceCapabilities

    original = SampleSource.read_page
    monkeypatch.setattr(
        SampleSource,
        "capabilities",
        property(
            lambda self: SourceCapabilities(cursor=True, time_semantics="interval", max_page_size=2)
        ),
    )

    def overlapping(self, *, start, end, cursor, limit):
        page = original(self, start=start, end=end, cursor=cursor, limit=limit)
        for record in page.records:
            record.effective_end = record.effective_at + timedelta(minutes=30)
        if cursor is None:
            page.records[0].effective_at = start - timedelta(minutes=30)
            page.records[0].effective_end = start + timedelta(minutes=30)
        return page

    monkeypatch.setattr(SampleSource, "read_page", overlapping)
    assert poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW) == {
        "status": "complete",
        "records": 3,
        "pages": 2,
    }
    db.expire_all()
    assert len(db.scalars(select(SourcePayload)).all()) == 3


def test_source_worker_uses_local_day_overlap_at_window_start(db, db_engine, monkeypatch):
    from zoneinfo import ZoneInfo

    from synthetic_adapters import SampleSource

    from garmin_ai.source_contracts import SourceCapabilities, SourcePage, SourceRecord

    zone = ZoneInfo("Pacific/Kiritimati")
    monkeypatch.setattr(
        SampleSource,
        "capabilities",
        property(lambda self: SourceCapabilities(time_semantics="calendar_day")),
    )

    def calendar_day(self, *, start, end, cursor, limit):
        return SourcePage(
            instance_id=self.instance_id,
            page_kind="partial",
            fetched_at=NOW,
            records=[
                SourceRecord(
                    source_record_id="day-1",
                    observed_at=NOW,
                    effective_at=datetime(2025, 12, 26, tzinfo=zone),
                    source_timezone="Pacific/Kiritimati",
                    source_reference="synthetic:day-1",
                )
            ],
        )

    monkeypatch.setattr(SampleSource, "read_page", calendar_day)
    assert poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW) == {
        "status": "complete",
        "records": 1,
        "pages": 1,
    }
    db.expire_all()
    assert db.scalar(select(SourcePayload)).source_key == "day-1"


def test_source_worker_rejects_undeclared_correction(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    settings = selected_settings()
    poll_source_instance(db_engine, settings, "source:sample:one", now=NOW)
    original = SampleSource.read_page

    def changed(self, *, start, end, cursor, limit):
        page = original(self, start=start, end=end, cursor=cursor, limit=limit)
        if page.records:
            page.records[0].payload["walk_minutes"] = 99
        return page

    monkeypatch.setattr(SampleSource, "read_page", changed)
    with pytest.raises(ValueError, match="without correction capability"):
        poll_source_instance(db_engine, settings, "source:sample:one", now=NOW)
    db.expire_all()
    assert len(db.scalars(select(SourcePayload)).all()) == 3


def test_source_worker_rejects_oversized_payload_without_cursor_advance(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    original = SampleSource.read_page

    def oversized(self, *, start, end, cursor, limit):
        page = original(self, start=start, end=end, cursor=cursor, limit=limit)
        page.records[0].payload["unbounded_text"] = "x" * 65_000
        return page

    monkeypatch.setattr(SampleSource, "read_page", oversized)
    with pytest.raises(ValidationError, match="byte limit"):
        poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW)
    db.expire_all()
    assert db.scalars(select(SourcePayload)).all() == []
    assert db.get(AppState, "source-plugin:cursor:source:sample:one") is None


def test_declared_correction_and_deletion_preserve_raw_history(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    from garmin_ai.source_contracts import SourceCapabilities, SourceRecord

    settings = selected_settings()
    poll_source_instance(db_engine, settings, "source:sample:one", now=NOW)
    original = SampleSource.read_page
    monkeypatch.setattr(
        SampleSource,
        "capabilities",
        property(
            lambda self: SourceCapabilities(
                cursor=True, corrections=True, deletions=True, max_page_size=2
            )
        ),
    )

    def changed(self, *, start, end, cursor, limit):
        page = original(self, start=start, end=end, cursor=cursor, limit=limit)
        if page.records:
            page.records[0].payload["walk_minutes"] = 99
        return page

    monkeypatch.setattr(SampleSource, "read_page", changed)
    assert poll_source_instance(db_engine, settings, "source:sample:one", now=NOW)["status"] == (
        "complete"
    )
    db.expire_all()
    assert len(db.scalars(select(SourcePayload)).all()) == 5

    def deleted(self, *, start, end, cursor, limit):
        page = original(self, start=start, end=end, cursor=cursor, limit=limit)
        page.records = [
            SourceRecord.model_validate(
                {
                    **page.records[0].model_dump(mode="json"),
                    "operation": "delete",
                    "payload": {},
                }
            )
        ]
        page.next_cursor = None
        return page

    monkeypatch.setattr(SampleSource, "read_page", deleted)
    assert poll_source_instance(db_engine, settings, "source:sample:one", now=NOW)["status"] == (
        "complete"
    )
    db.expire_all()
    assert len(db.scalars(select(SourcePayload)).all()) == 6
    history = db.scalars(select(SourcePayload).where(SourcePayload.source_key == "walk-1")).all()
    assert {row.payload["operation"] for row in history} == {"upsert", "delete"}
    identity = hashlib.sha256(b"walk-1").hexdigest()
    assert (
        db.get(AppState, f"source-plugin:record:source:sample:one:{identity}").value["operation"]
        == "delete"
    )


def test_source_worker_rejects_invalid_page_before_advancing_cursor(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    original = SampleSource.read_page

    def foreign(self, *, start, end, cursor, limit):
        page = original(self, start=start, end=end, cursor=cursor, limit=limit)
        page.instance_id = "source:sample:other"
        return page

    monkeypatch.setattr(SampleSource, "read_page", foreign)
    with pytest.raises(ValueError, match="different instance"):
        poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW)
    db.expire_all()
    assert db.scalars(select(SourcePayload)).all() == []
    assert db.get(AppState, "source-plugin:cursor:source:sample:one") is None


def test_paginated_final_page_cannot_claim_complete_snapshot(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    original = SampleSource.read_page

    def invalid_snapshot(self, *, start, end, cursor, limit):
        page = original(self, start=start, end=end, cursor=cursor, limit=limit)
        if cursor is not None and page.next_cursor is None:
            page.page_kind = "complete_interval_snapshot"
        return page

    monkeypatch.setattr(SampleSource, "read_page", invalid_snapshot)
    with pytest.raises(ValueError, match="cannot follow a pagination cursor"):
        poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW)
    db.expire_all()
    assert db.get(AppState, "source-plugin:cursor:source:sample:one").value["next_cursor"] == "2"
    assert len(db.scalars(select(SourcePayload)).all()) == 2


def test_source_worker_rejects_undeclared_deletion(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    from garmin_ai.source_contracts import SourceRecord

    original = SampleSource.read_page

    def deleted(self, *, start, end, cursor, limit):
        page = original(self, start=start, end=end, cursor=cursor, limit=limit)
        page.records = [
            SourceRecord.model_validate(
                {**page.records[0].model_dump(mode="json"), "operation": "delete", "payload": {}}
            )
        ]
        return page

    monkeypatch.setattr(SampleSource, "read_page", deleted)
    with pytest.raises(ValueError, match="undeclared deletions"):
        poll_source_instance(db_engine, selected_settings(), "source:sample:one", now=NOW)
    db.expire_all()
    assert db.scalars(select(SourcePayload)).all() == []


def test_concurrent_source_polls_only_advance_one_cursor(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    original = SampleSource.read_page
    barrier = threading.Barrier(2)

    def concurrent(self, *, start, end, cursor, limit):
        if cursor is None:
            barrier.wait(timeout=5)
        return original(self, start=start, end=end, cursor=cursor, limit=limit)

    monkeypatch.setattr(SampleSource, "read_page", concurrent)
    settings = selected_settings()
    db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(poll_source_instance, db_engine, settings, "source:sample:one", now=NOW)
            for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert {result["status"] for result in results} == {"complete", "stale"}
    db.expire_all()
    assert len(db.scalars(select(SourcePayload)).all()) == 3
    assert db.get(AppState, "source-plugin:cursor:source:sample:one").value["next_cursor"] is None


def test_source_instances_keep_separate_progress_and_records(db, db_engine):
    first = selected_settings().integrations[0]
    second = first.model_copy(update={"id": "source:sample:two", "config": {"label": "two"}})
    settings = Settings(integrations=[first, second])
    assert configured_source_plugins(settings) == (first.id, second.id)
    assert poll_source_instance(db_engine, settings, first.id, now=NOW, max_pages=1)["status"] == (
        "partial"
    )
    assert poll_source_instance(db_engine, settings, second.id, now=NOW)["status"] == ("complete")
    db.expire_all()
    assert db.get(AppState, f"source-plugin:cursor:{first.id}").value["next_cursor"] == "2"
    assert db.get(AppState, f"source-plugin:cursor:{second.id}").value["next_cursor"] is None
    rows = db.scalars(select(SourcePayload)).all()
    assert len(rows) == 5
    assert {row.source for row in rows} == {f"plugin:{first.id}", f"plugin:{second.id}"}


def test_source_retry_deadline_is_respected(db, db_engine, monkeypatch):
    from synthetic_adapters import SampleSource

    original = SampleSource.read_page
    calls = []

    def throttled(self, *, start, end, cursor, limit):
        calls.append(cursor)
        page = original(self, start=start, end=end, cursor=cursor, limit=limit)
        page.retry_after = NOW + timedelta(hours=2)
        return page

    monkeypatch.setattr(SampleSource, "read_page", throttled)
    settings = selected_settings()
    assert poll_source_instance(db_engine, settings, "source:sample:one", now=NOW)["status"] == (
        "deferred"
    )
    assert poll_source_instance(db_engine, settings, "source:sample:one", now=NOW)["status"] == (
        "deferred"
    )
    assert calls == [None]
    db.expire_all()
    assert len(db.scalars(select(SourcePayload)).all()) == 2
    assert db.get(AppState, "source-plugin:cursor:source:sample:one").value["next_cursor"] == "2"


def test_selected_source_runs_through_ordinary_runtime(db, db_engine, tmp_path, monkeypatch):
    from synthetic_adapters import SampleSource

    from garmin_ai import runtime

    called = threading.Event()
    original = SampleSource.read_page

    def tracked(self, *, start, end, cursor, limit):
        called.set()
        return original(self, start=start, end=end, cursor=cursor, limit=limit)

    monkeypatch.setattr(SampleSource, "read_page", tracked)
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)
    settings = Settings(
        integrations=selected_settings().integrations,
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="",
        llm_enabled=False,
    )
    db.commit()

    async def check():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "add_signal_handler",
            lambda _signal, callback: callbacks.append(callback),
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(called.wait, 3), 4)
            for _ in range(60):
                await asyncio.sleep(0.05)
                db.expire_all()
                if db.get(AppState, "source-plugin:cursor:source:sample:one") is not None:
                    break
            else:
                pytest.fail("Source cursor was not committed by the runtime")
            jobs = db.scalars(select(Job).where(Job.kind == "source_plugin_poll")).all()
            assert len(jobs) == 1
            assert not task.done()
        finally:
            if callbacks:
                callbacks[0]()
            await asyncio.wait_for(task, 5)

    asyncio.run(check())
