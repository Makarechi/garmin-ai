import asyncio
import base64
import json
import os
from datetime import UTC, datetime

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr
from sqlalchemy import func, select, text

from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import Base, Event, Measurement
from garmin_ai.operations import (
    create_backup,
    decrypt_file,
    encrypt_file,
    export_database,
    restore_database,
    unpack_backup,
)


def test_encryption_tamper_and_existing_destination(tmp_path):
    source, encrypted, restored = [tmp_path / p for p in ("plain", "encrypted", "restored")]
    source.write_bytes(os.urandom(2 * 1024 * 1024 + 19))
    key = os.urandom(32)
    encrypt_file(source, encrypted, key)
    decrypt_file(encrypted, restored, key)
    assert restored.read_bytes() == source.read_bytes()
    with pytest.raises(ValueError):
        decrypt_file(encrypted, restored, key)
    assert restored.exists()
    restored.unlink()
    damaged = bytearray(encrypted.read_bytes())
    damaged[100] ^= 1
    encrypted.write_bytes(damaged)
    with pytest.raises(InvalidTag):
        decrypt_file(encrypted, restored, key)
    assert not restored.exists()


def test_database_export_restore_and_backup_roundtrip(db, db_engine, tmp_path):
    event = create_event(
        db,
        EventInput(start="2026-09-07T12:00:00Z", payload={"type": "migraine", "severity": 5}),
        actor="test",
    )
    identity = event.id
    db.add(
        Measurement(
            ts=datetime(2026, 9, 7, tzinfo=UTC),
            metric="heart_rate_bpm",
            source="synthetic",
            local_date=datetime(2026, 9, 7).date(),
            value=60,
            unit="bpm",
        )
    )
    db.commit()
    exported = tmp_path / "export.gz"
    counts = export_database(db_engine, exported)
    with pytest.raises(ValueError):
        restore_database(db_engine, exported)
    names = ", ".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    assert restore_database(db_engine, exported) == counts
    db.expire_all()
    assert db.get(Event, identity).payload["severity"] == 5
    assert db.scalar(select(func.count()).select_from(Measurement)) == 1
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data",
        lock_dir=tmp_path / "locks",
        token_dir=tmp_path / "tokens",
        backup_key=SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode()),
    )
    (settings.data_dir / "raw").mkdir(parents=True)
    (settings.data_dir / "raw" / "synthetic.json").write_text('{"synthetic": true}')
    (settings.data_dir / "coverage-report.json").write_text('{"requests": []}')
    backup = tmp_path / "backup.enc"
    create_backup(db_engine, settings, backup)
    unpack_backup(settings, backup, tmp_path / "unpacked")
    assert (tmp_path / "unpacked/raw/synthetic.json").read_text() == '{"synthetic": true}'
    assert (tmp_path / "unpacked/coverage-report.json").read_text() == '{"requests": []}'
    assert backup.stat().st_mode & 0o777 == 0o600


def test_mcp_stdio_lists_and_executes_bounded_tools(db, db_engine):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check():
        env = {**os.environ, "GA_DATABASE_URL": db_engine.url.render_as_string(hide_password=False)}
        params = StdioServerParameters(
            command="uv", args=["run", "python", "-m", "garmin_ai.mcp_server"], env=env
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            listing = await client.list_tools()
            assert len(listing.tools) == 18
            assert next(
                t for t in listing.tools if t.name == "health_snapshot"
            ).annotations.readOnlyHint
            result = await client.call_tool("health_snapshot", {"day": "1900-01-01"})
            assert not result.isError
            assert json.loads(result.content[0].text)["available"] is False
            created = await client.call_tool(
                "events_create",
                {
                    "idempotency_key": "mcp-synthetic",
                    "event": {
                        "start": "2026-09-07T12:00:00Z",
                        "timezone": "America/New_York",
                        "payload": {"type": "migraine", "severity": 6, "aura": False},
                    },
                },
            )
            record = json.loads(created.content[0].text)
            edited = await client.call_tool(
                "events_update",
                {"event_id": record["id"], "revision": 1, "changes": {"payload": {"severity": 3}}},
            )
            updated = json.loads(edited.content[0].text)
            assert updated["payload"]["severity"] == 3 and updated["payload"]["aura"] is False
            assert updated["timezone"] == "America/New_York" and updated["source"] == "mcp"
            invalid = await client.call_tool("metric_series", {"metric": "invalid"})
            assert invalid.isError

    asyncio.run(check())


def test_export_preserves_existing_parent_permissions(db_engine, tmp_path):
    tmp_path.chmod(0o755)
    export_database(db_engine, tmp_path / "export.gz")
    assert tmp_path.stat().st_mode & 0o777 == 0o755


def test_erasure_blocks_future_service_writes(db, db_engine, tmp_path):
    from garmin_ai.db import MaintenanceMode, transaction
    from garmin_ai.operations import erase_all

    settings = Settings(
        data_dir=tmp_path / "data", lock_dir=tmp_path / "locks", token_dir=tmp_path / "tokens"
    )
    settings.data_dir.mkdir()
    settings.token_dir.mkdir()
    create_event(
        db, EventInput(start="2026-09-07T12:00:00Z", payload={"type": "migraine"}), actor="test"
    )
    db.commit()
    assert erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")["erased"]
    assert not settings.data_dir.exists()
    with pytest.raises(MaintenanceMode), transaction(db_engine):
        pass
    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_retention_keeps_newest_scheduled_snapshots_only(tmp_path):
    from garmin_ai.operations import prune_scheduled_backups

    for day in range(1, 6):
        (tmp_path / f"garmin-ai-2026-09-{day:02d}.enc").write_bytes(b"synthetic")
    (tmp_path / "manual.enc").write_bytes(b"synthetic")
    prune_scheduled_backups(tmp_path, 2)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "garmin-ai-2026-09-04.enc",
        "garmin-ai-2026-09-05.enc",
        "manual.enc",
    ]


def test_large_restore_batches_insert_roundtrips(db, db_engine, tmp_path):
    from datetime import timedelta

    from sqlalchemy import event as sql_event
    from sqlalchemy import insert

    instant = datetime(2026, 9, 7, tzinfo=UTC)
    rows = [
        dict(
            ts=instant + timedelta(seconds=i),
            metric="synthetic",
            source="test",
            local_date=instant.date(),
            value=1,
            unit="count",
        )
        for i in range(2501)
    ]
    with db_engine.begin() as conn:
        conn.execute(insert(Measurement), rows)
    path = tmp_path / "batch.gz"
    export_database(db_engine, path)
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE measurements"))
    inserts = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO measurements"):
            inserts.append(statement)

    sql_event.listen(db_engine, "before_cursor_execute", observe)
    try:
        counts = restore_database(db_engine, path)
    finally:
        sql_event.remove(db_engine, "before_cursor_execute", observe)
    assert counts["measurements"] == 2501 and 1 <= len(inserts) <= 4


def test_restore_accepts_only_erasure_marker(db, db_engine, tmp_path):
    from garmin_ai.models import AppState

    source = tmp_path / "empty.gz"
    export_database(db_engine, source)
    db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()
    assert restore_database(db_engine, source)["app_state"] == 0
    db.expire_all()
    assert db.get(AppState, "maintenance:erased") is None


def test_probe_guard_coordinates_erasure_and_maintenance(db, db_engine, tmp_path):
    from garmin_ai.db import MaintenanceMode, exclusive_ingestion
    from garmin_ai.models import AppState
    from garmin_ai.operations import erase_all

    settings = Settings(
        data_dir=tmp_path / "data", lock_dir=tmp_path / "locks", token_dir=tmp_path / "tokens"
    )
    with exclusive_ingestion(db_engine):
        with pytest.raises(ValueError, match="Stop the runtime"):
            erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
    db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()
    with pytest.raises(MaintenanceMode):
        with exclusive_ingestion(db_engine):
            raise AssertionError("Erased storage cannot be ingested")


def test_backup_directory_must_be_independent(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        Settings(data_dir=tmp_path, backup_dir=tmp_path / "backups")


def test_scheduled_backup_retry_reuses_authenticated_snapshot(db, db_engine, tmp_path, monkeypatch):
    from garmin_ai.operations import scheduled_backup

    settings = Settings(
        data_dir=tmp_path / "data",
        lock_dir=tmp_path / "locks",
        token_dir=tmp_path / "tokens",
        backup_dir=tmp_path / "backups",
        backup_key=base64.urlsafe_b64encode(os.urandom(32)).decode(),
    )
    target = settings.backup_dir / "garmin-ai-2026-09-08.enc"
    first = scheduled_backup(db_engine, settings, target)
    saved = target.read_bytes()

    def never_create(*args):
        raise AssertionError("Retry must reuse the completed snapshot")

    monkeypatch.setattr("garmin_ai.operations.create_backup", never_create)
    second = scheduled_backup(db_engine, settings, target)
    assert second <= first and target.read_bytes() == saved
    damaged = bytearray(saved)
    damaged[-1] ^= 1
    target.write_bytes(damaged)
    with pytest.raises(InvalidTag):
        scheduled_backup(db_engine, settings, target)


def test_login_and_probe_exclude_erasure_before_database_setup(
    db, db_engine, tmp_path, monkeypatch
):
    from garmin_ai import cli
    from garmin_ai.operations import erase_all
    from garmin_ai.storage_files import exclusive_files

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        database_url="",
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("builtins.input", lambda _: "synthetic@example.invalid")
    monkeypatch.setattr(cli, "getpass", lambda _: "synthetic")

    class Garmin:
        def __init__(self, **kwargs):
            self.client = self

        def login(self):
            with pytest.raises(ValueError, match="Stop the worker"):
                erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")

        def dump(self, path):
            from pathlib import Path

            (Path(path) / "synthetic-token").write_text("synthetic")

    monkeypatch.setattr(cli, "Garmin", Garmin)
    monkeypatch.setattr("sys.argv", ["garmin-ai", "login"])
    cli.main()
    assert (settings.token_dir / "synthetic-token").exists()

    def probe(*args, **kwargs):
        with pytest.raises(ValueError, match="Stop the worker"):
            erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
        return {"synthetic": True}

    monkeypatch.setattr(cli, "probe", probe)
    monkeypatch.setattr(cli.GarminReader, "restore", lambda _: object())
    monkeypatch.setattr("sys.argv", ["garmin-ai", "probe"])
    cli.main()
    assert (settings.data_dir / "coverage-report.json").exists()
    assert erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")["erased"]
    with pytest.raises(ValueError, match="resume"):
        with exclusive_files(settings):
            pytest.fail("Probe must remain disabled after erasure")
    # A deliberately requested login remains possible, but does not resume ingestion.
    with exclusive_files(settings, allow_erased=True):
        assert (settings.lock_dir / "erased").exists()


def test_backup_rename_is_durable_before_success(tmp_path, monkeypatch):
    import stat

    from garmin_ai import operations

    original_replace, original_fsync = os.replace, os.fsync
    calls = []

    def replace(*args):
        original_replace(*args)
        calls.append("rename")

    def fsync(descriptor):
        calls.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        original_fsync(descriptor)

    monkeypatch.setattr(operations.os, "replace", replace)
    monkeypatch.setattr(operations.os, "fsync", fsync)
    source = tmp_path / "source"
    source.write_bytes(b"synthetic")
    encrypt_file(source, tmp_path / "backup.enc", os.urandom(32))
    assert calls == ["file", "rename", "directory"]


@pytest.mark.parametrize(
    "data,tokens", [("same", "same"), ("tokens/data", "tokens"), ("data", "data/tokens")]
)
def test_data_and_token_roots_cannot_overlap(tmp_path, data, tokens):
    with pytest.raises(ValueError, match="overlap"):
        Settings(data_dir=tmp_path / data, token_dir=tmp_path / tokens)


def test_manual_backup_holds_file_lock_during_snapshot(db, db_engine, tmp_path, monkeypatch):
    from garmin_ai import cli, operations
    from garmin_ai.storage_files import exclusive_files

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        database_url="",
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("garmin_ai.db.make_engine", lambda *args: db_engine)

    def backup(*args):
        with pytest.raises(ValueError, match="Stop the worker"):
            with exclusive_files(settings, allow_erased=True):
                pytest.fail("Concurrent file writer must be excluded")
        return {"synthetic": True}

    monkeypatch.setattr(operations, "create_backup", backup)
    monkeypatch.setattr("sys.argv", ["garmin-ai", "backup", str(tmp_path / "backup.enc")])
    cli.main()


def test_existing_lock_directory_mode_survives_locking_and_erasure(db, db_engine, tmp_path):
    from garmin_ai.operations import erase_all
    from garmin_ai.storage_files import exclusive_files

    directory = tmp_path / "shared"
    directory.mkdir(mode=0o1777)
    directory.chmod(0o1777)
    settings = Settings(
        data_dir=tmp_path / "data", token_dir=tmp_path / "tokens", lock_dir=directory
    )
    with exclusive_files(settings):
        assert directory.stat().st_mode & 0o7777 == 0o1777
    erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
    assert directory.stat().st_mode & 0o7777 == 0o1777
    assert (directory / "erased").stat().st_mode & 0o777 == 0o600


def test_inventory_import_does_not_require_unix_lock_module():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['fcntl']=None; from garmin_ai.cli import main; sys.argv=['garmin-ai','inventory']; main()",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and "activities" in result.stdout


def test_cancelled_backup_thread_keeps_file_lock(tmp_path):
    import threading

    from garmin_ai.runtime import run_blocking
    from garmin_ai.storage_files import exclusive_files

    settings = Settings(lock_dir=tmp_path / "locks")
    entered, release = threading.Event(), threading.Event()

    def work():
        entered.set()
        release.wait(timeout=5)

    async def guarded():
        with exclusive_files(settings):
            await run_blocking(work)

    async def check():
        task = asyncio.create_task(guarded())
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        with pytest.raises(ValueError, match="Stop the worker"):
            with exclusive_files(settings):
                pytest.fail("Lock released while thread is active")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with exclusive_files(settings):
            pass

    asyncio.run(check())


def test_export_flushes_file_and_directory_before_success(db, db_engine, tmp_path, monkeypatch):
    import stat

    from garmin_ai import operations

    original_replace, original_fsync = os.replace, os.fsync
    calls = []

    def replace(*args):
        original_replace(*args)
        calls.append("rename")

    def fsync(descriptor):
        calls.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        original_fsync(descriptor)

    monkeypatch.setattr(operations.os, "replace", replace)
    monkeypatch.setattr(operations.os, "fsync", fsync)
    export_database(db_engine, tmp_path / "export.gz")
    assert calls == ["file", "rename", "directory"]


def test_windows_directory_flush_does_not_open_directory(tmp_path, monkeypatch):
    from garmin_ai import archive

    with monkeypatch.context() as patch:
        patch.setattr(archive.os, "name", "nt")
        patch.setattr(
            archive.os,
            "open",
            lambda *args: (_ for _ in ()).throw(AssertionError("directory opened")),
        )
        archive.fsync_directory(tmp_path)


@pytest.mark.parametrize("relative", ["data", "data/nested", "tokens", "tokens/nested"])
def test_unpack_cannot_repopulate_protected_roots(tmp_path, relative):
    settings = Settings(data_dir=tmp_path / "data", token_dir=tmp_path / "tokens")
    with pytest.raises(ValueError, match="separate recovery"):
        unpack_backup(settings, tmp_path / "not-needed.enc", tmp_path / relative)


def test_existing_backup_is_preserved_without_explicit_overwrite(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "backup.enc"
    source.write_bytes(b"synthetic-new-data")
    destination.write_bytes(b"synthetic-old-backup")
    before = destination.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        encrypt_file(source, destination, os.urandom(32))
    with pytest.raises(ValueError, match="already exists"):
        create_backup(None, Settings(), destination)
    assert destination.read_bytes() == before


def test_erasure_flushes_both_source_parent_directories(db, db_engine, tmp_path, monkeypatch):
    from garmin_ai import operations

    settings = Settings(
        data_dir=tmp_path / "data-parent/data",
        token_dir=tmp_path / "token-parent/tokens",
        lock_dir=tmp_path / "locks",
    )
    settings.data_dir.mkdir(parents=True)
    settings.token_dir.mkdir(parents=True)
    (settings.data_dir / "synthetic").write_bytes(b"synthetic")
    (settings.token_dir / "synthetic").write_bytes(b"synthetic")
    flushed = []
    original = operations.fsync_directory

    def flush(path):
        assert not settings.data_dir.exists() if path == settings.data_dir.parent else True
        assert not settings.token_dir.exists() if path == settings.token_dir.parent else True
        original(path)
        flushed.append(path)

    monkeypatch.setattr(operations, "fsync_directory", flush)
    operations.erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
    assert set(flushed) >= {settings.data_dir.parent, settings.token_dir.parent}
