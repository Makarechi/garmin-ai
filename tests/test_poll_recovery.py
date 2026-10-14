import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import TimedOut

from garmin_ai.config import Settings
from garmin_ai.models import AppState
from garmin_ai.telegram import poll


def test_slow_scheduler_database_work_does_not_block_network_loop(
    db, db_engine, tmp_path, monkeypatch
):
    from garmin_ai import conversation, runtime

    entered = threading.Event()
    release = threading.Event()
    default_pool_entered = threading.Event()
    release_default_pool = threading.Event()

    def slow_prune(*_args):
        entered.set()
        assert release.wait(10)

    def occupy_default_pool():
        default_pool_entered.set()
        assert release_default_pool.wait(10)

    monkeypatch.setattr(conversation, "prune_conversation", slow_prune)
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)
    monkeypatch.setattr(runtime, "claim_ready_job", lambda *_args: None)
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="",
        llm_enabled=False,
    )
    db.commit()

    async def run():
        loop = asyncio.get_running_loop()
        callbacks = []
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda _signal, callback: callbacks.append(callback),
        )
        shared_executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(shared_executor)
        blocked = loop.run_in_executor(None, occupy_default_pool)
        task = None
        try:
            assert default_pool_entered.wait(1)
            task = asyncio.create_task(runtime.run(settings))
            for _ in range(250):
                if entered.is_set():
                    break
                await asyncio.sleep(0.02)
            assert entered.is_set()
            # The loop must still be able to service a Telegram network callback.
            await asyncio.wait_for(asyncio.sleep(0.01), 0.5)
            assert not task.done()
        finally:
            release.set()
            release_default_pool.set()
            if callbacks:
                callbacks[0]()
            if task is not None:
                await asyncio.wait_for(task, 5)
            await blocked
            shared_executor.shutdown(wait=True)

    asyncio.run(run())


def test_slow_claim_does_not_block_network_event_loop(db_engine, monkeypatch):
    import threading

    from garmin_ai import runtime

    entered = threading.Event()
    release = threading.Event()

    def slow_claim(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return None

    monkeypatch.setattr(runtime, "claim", slow_claim)

    async def run():
        task = asyncio.create_task(
            runtime.run_blocking(
                runtime.claim_ready_job, db_engine, ["telegram_update"], False, False
            )
        )
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            # A network callback can run while the database query is still busy.
            assert not task.done()
        finally:
            release.set()
            await task

    asyncio.run(run())


def test_three_timeouts_reset_only_polling_transport_without_losing_offset(
    db, db_engine, monkeypatch
):
    db.add(
        AppState(
            key="telegram:offset",
            value={
                "offset": 901,
                "received_at": datetime.now(UTC).isoformat(),
            },
        )
    )
    db.commit()
    transport = SimpleNamespace(shutdown=AsyncMock(), initialize=AsyncMock())
    offsets = []

    async def run():
        stop = asyncio.Event()

        async def get_updates(**kwargs):
            offsets.append(kwargs["offset"])
            if len(offsets) <= 3:
                raise TimedOut()
            stop.set()
            return []

        monkeypatch.setattr("garmin_ai.telegram.asyncio.sleep", AsyncMock())
        await poll(
            SimpleNamespace(get_updates=get_updates),
            db_engine,
            Settings(),
            stop,
            polling_request=transport,
        )

    asyncio.run(run())
    assert offsets == [901] * 4
    transport.shutdown.assert_awaited_once()
    transport.initialize.assert_awaited_once()


def test_successful_poll_breaks_consecutive_failure_count(db, db_engine, monkeypatch):
    transport = SimpleNamespace(shutdown=AsyncMock(), initialize=AsyncMock())

    async def run():
        stop = asyncio.Event()
        calls = 0

        async def get_updates(**kwargs):
            nonlocal calls
            calls += 1
            if calls in {1, 2, 4, 5}:
                raise TimedOut()
            if calls == 6:
                stop.set()
            return []

        monkeypatch.setattr("garmin_ai.telegram.asyncio.sleep", AsyncMock())
        await poll(
            SimpleNamespace(get_updates=get_updates),
            db_engine,
            Settings(),
            stop,
            polling_request=transport,
        )

    asyncio.run(run())
    transport.shutdown.assert_not_awaited()
