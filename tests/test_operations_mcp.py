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
        env = {
            **os.environ,
            "GA_DATABASE_URL": db_engine.url.render_as_string(hide_password=False),
            "GA_MCP_ENABLE_WRITES": "true",
        }
        params = StdioServerParameters(
            command="uv", args=["run", "python", "-m", "garmin_ai.mcp_server"], env=env
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            listing = await client.list_tools()
            from garmin_ai.mcp_server import WRITES
            from garmin_ai.tools import TOOLS

            assert {tool.name for tool in listing.tools} == set(TOOLS) | set(WRITES)
            wellbeing = await client.call_tool(
                "wellbeing_observations",
                {"start": "1900-01-01T00:00:00Z", "end": "1900-01-02T00:00:00Z"},
            )
            assert not wellbeing.isError
            assert json.loads(wellbeing.content[0].text)["missingness"] == "unreported_is_unknown"
            sleep = await client.call_tool(
                "analysis_sleep", {"start": "1900-01-01", "end": "1900-01-01"}
            )
            assert not sleep.isError
            assert json.loads(sleep.content[0].text)["summary"]["available_sleep_days"] == 0
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

            (Path(path) / "garmin_tokens.json").write_text("synthetic")

    monkeypatch.setattr(cli, "Garmin", Garmin)
    monkeypatch.setattr("sys.argv", ["garmin-ai", "login"])
    cli.main()
    assert (settings.token_dir / "garmin_tokens.json").exists()

    def probe(*args, **kwargs):
        with pytest.raises(ValueError, match="Stop the worker"):
            erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
        return {"synthetic": True}

    monkeypatch.setattr(cli, "probe", probe)
    from types import SimpleNamespace

    monkeypatch.setattr(
        cli.GarminReader, "restore", lambda _: SimpleNamespace(account_fingerprint=lambda: "a" * 64)
    )
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


def test_backup_publication_is_durable_before_success(tmp_path, monkeypatch):
    import stat

    from garmin_ai import operations

    original_link, original_fsync = os.link, os.fsync
    calls = []

    def link(*args):
        original_link(*args)
        calls.append("publish")

    def fsync(descriptor):
        calls.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        original_fsync(descriptor)

    monkeypatch.setattr(operations.os, "link", link)
    monkeypatch.setattr(operations.os, "fsync", fsync)
    source = tmp_path / "source"
    source.write_bytes(b"synthetic")
    encrypt_file(source, tmp_path / "backup.enc", os.urandom(32))
    assert calls == ["directory", "file", "publish", "directory", "directory"]


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

    original_link, original_fsync = os.link, os.fsync
    calls = []

    def link(*args):
        original_link(*args)
        calls.append("publish")

    def fsync(descriptor):
        calls.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        original_fsync(descriptor)

    monkeypatch.setattr(operations.os, "link", link)
    monkeypatch.setattr(operations.os, "fsync", fsync)
    export_database(db_engine, tmp_path / "export.gz")
    assert calls == ["directory", "file", "publish", "directory", "directory"]


def test_windows_directory_flush_uses_native_barrier(tmp_path, monkeypatch):
    from garmin_ai import archive

    flushed = []
    with monkeypatch.context() as patch:
        patch.setattr(archive.os, "name", "nt")
        patch.setattr(archive, "flush_windows_volume", flushed.append)
        patch.setattr(
            archive.os,
            "open",
            lambda *args: (_ for _ in ()).throw(AssertionError("directory opened")),
        )
        archive.fsync_directory(tmp_path)
    assert flushed == [tmp_path]


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


def test_erased_storage_cannot_publish_export(db, db_engine, tmp_path):
    from garmin_ai.models import AppState

    db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()
    destination = tmp_path / "export.gz"
    with pytest.raises(ValueError, match="erased storage"):
        export_database(db_engine, destination)
    assert list(tmp_path.iterdir()) == []


def test_legacy_erased_export_cannot_disable_restored_storage(db, db_engine, tmp_path):
    import gzip

    from garmin_ai.models import AppState
    from garmin_ai.operations import REVISION

    destination = tmp_path / "legacy.gz"
    records = [
        {"format": "garmin-ai-jsonl-v1", "revision": REVISION},
        {"table": "app_state", "row": {"key": "maintenance:erased", "value": {"disabled": True}}},
        {"counts": {t.name: int(t.name == "app_state") for t in Base.metadata.sorted_tables}},
    ]
    with gzip.open(destination, "wt") as stream:
        stream.write("\n".join(json.dumps(row) for row in records))
    with pytest.raises(ValueError, match="erased storage"):
        restore_database(db_engine, destination)
    assert db.get(AppState, "maintenance:erased") is None


@pytest.mark.parametrize("source", ["data", "raw", "tokens"])
def test_backup_refuses_symlink_source_roots_before_staging(tmp_path, source):
    settings = Settings(data_dir=tmp_path / "data", token_dir=tmp_path / "tokens")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (outside / "private.txt").write_text("synthetic private content")
    link = {
        "data": settings.data_dir,
        "raw": settings.data_dir / "raw",
        "tokens": settings.token_dir,
    }[source]
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    backup = tmp_path / "backup.enc"
    with pytest.raises(ValueError, match="source root is a symlink"):
        create_backup(None, settings, backup)
    assert not backup.exists()
    assert list(outside.iterdir()) == [outside / "private.txt"]
    assert (outside / "private.txt").read_text() == "synthetic private content"
    assert outside.stat().st_mode & 0o777 == 0o755


def test_clock_rollback_retention_preserves_successful_snapshot(db, db_engine, tmp_path):
    from garmin_ai.operations import scheduled_backup

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        backup_keep_daily=2,
        backup_key=SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode()),
    )
    backups = tmp_path / "backups"
    backups.mkdir()
    for day in (10, 11):
        (backups / f"garmin-ai-2026-09-{day}.enc").write_bytes(b"synthetic-future-snapshot")
    destination = backups / "garmin-ai-2026-09-07.enc"
    completed = scheduled_backup(db_engine, settings, destination)
    assert destination.exists()
    assert completed == datetime.fromtimestamp(destination.stat().st_mtime, UTC)
    assert len(list(backups.glob("*.enc"))) == 2
    unpack_backup(settings, destination, tmp_path / "verified")
    assert (tmp_path / "verified/database.jsonl.gz").exists()


def test_export_waits_for_restore_before_establishing_snapshot(db, db_engine, tmp_path):
    import gzip
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    from garmin_ai.models import AppState

    destination = tmp_path / "snapshot.gz"
    with ThreadPoolExecutor(max_workers=1) as executor:
        with db_engine.begin() as restoring:
            restoring.execute(text("SELECT pg_advisory_xact_lock(72104622)"))
            export = executor.submit(export_database, db_engine, destination)
            with pytest.raises(TimeoutError):
                export.result(timeout=0.1)
            restoring.execute(
                AppState.__table__.insert().values(
                    key="synthetic:restored", value={"restored": True}
                )
            )
        export.result(timeout=5)
    with gzip.open(destination, "rt") as stream:
        records = [json.loads(line) for line in stream]
    assert any(r.get("row", {}).get("key") == "synthetic:restored" for r in records)


def test_login_lock_error_explains_safe_recovery_without_sensitive_details(monkeypatch, capsys):
    from garmin_ai import cli

    def busy(*args, **kwargs):
        raise ValueError("synthetic sensitive details")

    monkeypatch.setattr(cli, "standalone_files", busy)
    monkeypatch.setattr("sys.argv", ["garmin-ai", "login"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    output = capsys.readouterr().err
    assert (
        error.value.code == 1
        and "docker compose stop worker" in output
        and "docker compose start worker" in output
    )
    assert "synthetic sensitive" not in output


@pytest.mark.parametrize("command", ["restore-db", "resume-storage"])
@pytest.mark.parametrize("failure", ["unlink", "fsync"])
def test_activation_cleanup_failure_keeps_database_disabled(
    db, db_engine, tmp_path, monkeypatch, command, failure
):
    from pathlib import Path

    from garmin_ai import cli
    from garmin_ai.models import AppState

    source = tmp_path / "empty.gz"
    db.add(AppState(key="synthetic:restore", value={"synthetic": True}))
    db.commit()
    export_database(db_engine, source)
    db.delete(db.get(AppState, "synthetic:restore"))
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data", token_dir=tmp_path / "tokens", lock_dir=tmp_path / "locks"
    )
    settings.lock_dir.mkdir()
    marker = settings.lock_dir / "erased"
    marker.write_text("synthetic")
    db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("garmin_ai.db.make_engine", lambda _: db_engine)
    monkeypatch.setattr(
        "sys.argv", ["garmin-ai", command] + ([str(source)] if command == "restore-db" else [])
    )
    original_unlink = Path.unlink
    original_sync = cli.fsync_directory

    def unlink(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("synthetic cleanup failure")
        return original_unlink(path, *args, **kwargs)

    def sync(path):
        raise OSError("synthetic fsync failure")

    if failure == "unlink":
        monkeypatch.setattr(Path, "unlink", unlink)
    else:
        monkeypatch.setattr(cli, "fsync_directory", sync)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    db.expire_all()
    assert db.get(AppState, "synthetic:restore") is None
    assert db.get(AppState, "maintenance:erased") is not None
    db.rollback()
    monkeypatch.setattr(Path, "unlink", original_unlink)
    monkeypatch.setattr(cli, "fsync_directory", original_sync)
    cli.main()
    db.expire_all()
    assert db.get(AppState, "maintenance:erased") is None and not marker.exists()


@pytest.mark.parametrize("dangling", [False, True])
def test_scheduled_backup_rejects_symlink_recovery(db, db_engine, tmp_path, dangling):
    from garmin_ai.operations import scheduled_backup

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        backup_key=SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode()),
    )
    old = tmp_path / "old.enc"
    if not dangling:
        create_backup(db_engine, settings, old)
    target = tmp_path / "garmin-ai-2026-09-08.enc"
    target.symlink_to(old)
    with pytest.raises(ValueError, match="symlink"):
        scheduled_backup(db_engine, settings, target)
    assert target.is_symlink()


@pytest.mark.parametrize("destination_kind", ["file", "symlink", "dangling", "raced"])
def test_export_never_replaces_existing_destination(
    db_engine, tmp_path, monkeypatch, destination_kind
):
    from garmin_ai import operations

    target = tmp_path / "export.gz"
    existing = tmp_path / "existing"
    if destination_kind == "file":
        target.write_bytes(b"keep")
    elif destination_kind in {"symlink", "dangling"}:
        if destination_kind == "symlink":
            existing.write_bytes(b"keep")
        target.symlink_to(existing)
    else:
        original_link = operations.os.link

        def race(source, destination):
            target.write_bytes(b"keep")
            original_link(source, destination)

        monkeypatch.setattr(operations.os, "link", race)
    with pytest.raises((ValueError, FileExistsError)):
        export_database(db_engine, target)
    if destination_kind in {"symlink", "dangling"}:
        assert target.is_symlink()
    else:
        assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("cancel", [False, True])
def test_mcp_cancellation_keeps_transaction_attached(db, db_engine, monkeypatch, cancel):
    import threading

    import anyio
    from mcp import types

    from garmin_ai import mcp_server

    original = mcp_server.create_event
    release = threading.Event()

    async def scenario():
        started = anyio.Event()
        done = anyio.Event()
        scopes = []

        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            anyio.from_thread.run_sync(started.set)
            assert release.wait(5)
            return result

        monkeypatch.setattr(mcp_server, "create_event", delayed)
        server = mcp_server.build_server(db_engine, enable_writes=True)
        request = types.CallToolRequest(
            params=types.CallToolRequestParams(
                name="events_create",
                arguments={
                    "idempotency_key": "cancel-test",
                    "event": {
                        "start": "2026-09-07T12:00:00Z",
                        "timezone": "UTC",
                        "payload": {"type": "note", "description": "synthetic"},
                    },
                },
            )
        )

        async def request_task():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                result = await server.request_handlers[types.CallToolRequest](request)
                assert not result.root.isError
            done.set()

        async with anyio.create_task_group() as group:
            group.start_soon(request_task)
            await started.wait()
            if cancel:
                scopes[0].cancel()
            await anyio.sleep(0.03)
            assert not done.is_set()
            release.set()
            await done.wait()

    asyncio.run(scenario())
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Event)) == (0 if cancel else 1)


def test_unpack_flushes_contents_and_tree_before_success(db, db_engine, tmp_path, monkeypatch):
    import stat
    from pathlib import Path

    from garmin_ai import operations

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        backup_key=SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode()),
    )
    (settings.token_dir / "nested").mkdir(parents=True)
    (settings.token_dir / "nested" / "synthetic").write_text("synthetic")
    encrypted = tmp_path / "backup.enc"
    create_backup(db_engine, settings, encrypted)
    calls = []
    original_fsync, original_replace = operations.os.fsync, operations.publish_directory

    def fsync(descriptor):
        calls.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        original_fsync(descriptor)

    def replace(source, destination):
        if Path(destination) == tmp_path / "restored":
            calls.append("publish")
        return original_replace(source, destination)

    monkeypatch.setattr(operations.os, "fsync", fsync)
    monkeypatch.setattr(operations, "publish_directory", replace)
    unpack_backup(settings, encrypted, tmp_path / "restored")
    publication = calls.index("publish")
    assert calls[:publication].count("file") == 2
    assert calls[:publication].count("directory") >= 3
    assert calls[-1] == "directory"
    assert (tmp_path / "restored/tokens/nested/synthetic").read_text() == "synthetic"


def test_encrypted_backup_preserves_concurrent_destination(tmp_path, monkeypatch):
    from garmin_ai import operations

    source = tmp_path / "source"
    source.write_bytes(b"synthetic")
    destination = tmp_path / "backup.enc"
    original_link = operations.os.link

    def race(source, target):
        destination.write_bytes(b"preserve-existing")
        original_link(source, target)

    monkeypatch.setattr(operations.os, "link", race)
    with pytest.raises(FileExistsError):
        encrypt_file(source, destination, os.urandom(32))
    assert destination.read_bytes() == b"preserve-existing"
    assert set(tmp_path.iterdir()) == {source, destination}


@pytest.mark.parametrize("kind", ["directory", "dangling_symlink"])
def test_unpack_preserves_concurrent_destination(db, db_engine, tmp_path, monkeypatch, kind):
    from garmin_ai import operations

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        backup_key=SecretStr(base64.urlsafe_b64encode(os.urandom(32)).decode()),
    )
    source, destination = tmp_path / "backup.enc", tmp_path / "recovered"
    create_backup(db_engine, settings, source)
    publish = operations.publish_directory
    reserved = []

    def race(source, target):
        if kind == "directory":
            target.mkdir(mode=0o750)
        else:
            target.symlink_to(tmp_path / "absent")
        reserved.append(target.lstat())
        publish(source, target)

    monkeypatch.setattr(operations, "publish_directory", race)
    with pytest.raises(FileExistsError):
        unpack_backup(settings, source, destination)
    assert destination.lstat().st_ino == reserved[0].st_ino
    assert destination.lstat().st_mode == reserved[0].st_mode
    assert (
        destination.is_symlink()
        if kind == "dangling_symlink"
        else list(destination.iterdir()) == []
    )


def test_mcp_session_uses_explicit_timezone(db_engine, monkeypatch):
    import asyncio

    from mcp import types

    from garmin_ai import mcp_server

    monkeypatch.setattr(
        mcp_server, "call_tool", lambda session, *args: {"timezone": session.info["timezone"]}
    )
    server = mcp_server.build_server(db_engine, "America/Los_Angeles")
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(name="data_freshness", arguments={})
    )
    result = asyncio.run(server.request_handlers[types.CallToolRequest](request))
    assert not result.root.isError
    assert result.root.structuredContent["timezone"] == "America/Los_Angeles"


@pytest.mark.parametrize("command", ["resume-storage", "restore-db"])
@pytest.mark.parametrize("previously_erased", [False, True])
def test_activation_commit_failure_restores_local_fence(
    db, db_engine, tmp_path, monkeypatch, command, previously_erased
):
    from sqlalchemy import event

    from garmin_ai import cli
    from garmin_ai.models import AppState
    from garmin_ai.storage_files import standalone_files

    source = tmp_path / "empty.gz"
    export_database(db_engine, source)
    if previously_erased:
        db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        database_url="",
    )
    settings.lock_dir.mkdir()
    marker = settings.lock_dir / "erased"
    if previously_erased:
        marker.write_text("synthetic")
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("garmin_ai.db.make_engine", lambda _: db_engine)
    monkeypatch.setattr(
        "sys.argv", ["garmin-ai", command] + ([str(source)] if command == "restore-db" else [])
    )

    def fail_commit(connection):
        if not marker.exists():
            raise OSError("synthetic commit failure")

    event.listen(db_engine, "commit", fail_commit)
    try:
        with pytest.raises(SystemExit):
            cli.main()
    finally:
        event.remove(db_engine, "commit", fail_commit)
    db.expire_all()
    assert db.get(AppState, "maintenance:erased") is not None
    assert marker.exists() and marker.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        with standalone_files(settings):
            pytest.fail("An erased store must remain blocked without database settings")

    from garmin_ai.db import MaintenanceMode, transaction

    with pytest.raises(MaintenanceMode):
        with transaction(db_engine):
            pytest.fail("API and MCP database writers must remain blocked")


@pytest.mark.parametrize("command", ["resume-storage", "restore-db"])
@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
def test_activation_process_death_keeps_a_durable_fence(
    db, db_engine, tmp_path, monkeypatch, command, phase
):
    import subprocess
    import sys

    from garmin_ai import cli
    from garmin_ai.models import AppState
    from garmin_ai.storage_files import standalone_files

    source = tmp_path / "empty.gz"
    export_database(db_engine, source)
    db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        database_url="",
    )
    settings.lock_dir.mkdir()
    (settings.lock_dir / "erased").write_text("synthetic")
    code = """
import os,sys
from pathlib import Path
from sqlalchemy import event
from garmin_ai import cli,db
from garmin_ai.config import Settings
root=Path(sys.argv[1]); command=sys.argv[2]; phase=sys.argv[3]
settings=Settings(data_dir=root/'data',token_dir=root/'tokens',lock_dir=root/'locks')
engine=db.make_engine(settings)
cli.Settings=lambda:settings
db.make_engine=lambda _:engine
if phase=='before_commit':
    event.listen(engine,'commit',lambda connection:os._exit(17))
else:
    original=Path.unlink
    def unlink(path,*args,**kwargs):
        if path==settings.lock_dir/'activating':os._exit(17)
        return original(path,*args,**kwargs)
    Path.unlink=unlink
sys.argv=['garmin-ai',command]+([str(root/'empty.gz')] if command=='restore-db' else [])
cli.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), command, phase],
        env={**os.environ, "GA_DATABASE_URL": db_engine.url.render_as_string(hide_password=False)},
        capture_output=True,
    )
    assert result.returncode == 17
    db.expire_all()
    assert (db.get(AppState, "maintenance:erased") is not None) is (phase == "before_commit")
    assert (settings.lock_dir / "activating").is_file()
    assert not (settings.lock_dir / "erased").exists()
    db.rollback()
    with pytest.raises(ValueError):
        with standalone_files(settings):
            pytest.fail("Interrupted activation must block standalone files")
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("garmin_ai.db.make_engine", lambda _: db_engine)
    monkeypatch.setattr("sys.argv", ["garmin-ai", "resume-storage"])
    cli.main()
    assert not (settings.lock_dir / "activating").exists()
    with standalone_files(settings):
        pass


@pytest.mark.parametrize("machine,number", [("x86_64", 316), ("aarch64", 276)])
def test_exclusive_publish_uses_syscall_without_libc_wrapper(
    tmp_path, monkeypatch, machine, number
):
    from types import SimpleNamespace

    from garmin_ai import operations

    calls = []

    class Syscall:
        def __call__(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(operations.sys, "platform", "linux")
    monkeypatch.setattr(operations.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        operations.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(syscall=Syscall())
    )
    operations.publish_directory(tmp_path / "source", tmp_path / "destination")
    assert calls == [
        (
            number,
            -100,
            os.fsencode(tmp_path / "source"),
            -100,
            os.fsencode(tmp_path / "destination"),
            1,
        )
    ]


@pytest.mark.parametrize("fail", [False, True])
def test_activation_preserves_existing_lock_directory_permissions(tmp_path, fail):
    from garmin_ai.cli import activating_storage

    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(0o1777)
    settings = Settings(lock_dir=directory)
    (directory / "erased").write_text("synthetic")
    try:
        with activating_storage(settings) as activate:
            activate()
            assert directory.stat().st_mode & 0o7777 == 0o1777
            if fail:
                raise RuntimeError("synthetic")
    except RuntimeError:
        assert fail
    assert directory.stat().st_mode & 0o7777 == 0o1777
    if fail:
        assert (directory / "erased").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("garmin_activities", {"offset": 100}),
        ("garmin_endpoint", {"endpoint": "heart_rate"}),
        ("garmin_fit", {"activity_id": "synthetic"}),
    ],
)
def test_daily_backup_waits_for_all_sync_backlog(db, kind, payload):
    from datetime import UTC, datetime, timedelta

    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    backup = enqueue(db, "backup", {}, "synthetic-backup", now)
    dependency = enqueue(db, kind, payload, "synthetic-sync", now + timedelta(minutes=20))
    assert claim(db, now=now, kinds=["backup"]) is None
    assert db.get(Job, backup).attempts == 0
    db.get(Job, dependency).status = "done"
    db.flush()
    assert claim(db, now=now, kinds=["backup"]).id == backup


def test_erasure_process_death_before_commit_keeps_local_fence(db, db_engine, tmp_path):
    import subprocess
    import sys
    import time

    from garmin_ai.operations import erase_all
    from garmin_ai.storage_files import standalone_files

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        database_url="",
    )
    settings.data_dir.mkdir()
    (settings.data_dir / "synthetic").write_text("synthetic")
    code = """
import os,sys
from pathlib import Path
from sqlalchemy import event
from garmin_ai.config import Settings
from garmin_ai.db import make_engine
from garmin_ai.operations import erase_all
root=Path(sys.argv[1])
s=Settings(data_dir=root/'data',token_dir=root/'tokens',lock_dir=root/'locks')
e=make_engine(s)
def kill_at_erasure_commit(connection):
    if (s.lock_dir/'erased').exists():os._exit(17)
event.listen(e,'commit',kill_at_erasure_commit)
erase_all(e,s,'ERASE ALL LOCAL HEALTH DATA')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env={**os.environ, "GA_DATABASE_URL": db_engine.url.render_as_string(hide_password=False)},
        capture_output=True,
    )
    assert result.returncode == 17
    assert (settings.lock_dir / "erased").is_file()
    assert (settings.data_dir / "synthetic").is_file()
    with pytest.raises(ValueError):
        with standalone_files(settings):
            pytest.fail("Interrupted erasure must block file-only ingestion")
    # PostgreSQL may observe the dead client's socket shortly after process exit.
    for _ in range(100):
        try:
            erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
            break
        except ValueError as error:
            assert "Stop the runtime" in str(error)
            time.sleep(0.02)
    else:
        pytest.fail("Dead process retained its database lock")
    assert not settings.data_dir.exists()


@pytest.mark.parametrize("operation", ["encrypt", "export"])
@pytest.mark.parametrize("race", [False, True])
def test_publish_on_filesystem_without_hard_links(
    db, db_engine, tmp_path, monkeypatch, operation, race
):
    import errno
    import gzip

    from garmin_ai import operations

    source = tmp_path / "source"
    source.write_bytes(b"synthetic")
    destination = tmp_path / "published"
    key = os.urandom(32)

    def unsupported(source, target):
        if race:
            destination.write_bytes(b"preserve-existing")
        raise OSError(errno.EOPNOTSUPP, "Hard links unsupported")

    monkeypatch.setattr(operations.os, "link", unsupported)

    def publish():
        if operation == "encrypt":
            encrypt_file(source, destination, key)
        else:
            export_database(db_engine, destination)

    if race:
        with pytest.raises(FileExistsError):
            publish()
        assert destination.read_bytes() == b"preserve-existing"
    else:
        publish()
        if operation == "encrypt":
            restored = tmp_path / "restored"
            operations.decrypt_file(destination, restored, key)
            assert restored.read_bytes() == b"synthetic"
            restored.unlink()
        else:
            with gzip.open(destination, "rt") as stream:
                assert json.loads(next(stream))["format"] == "garmin-ai-jsonl-v1"
    assert set(tmp_path.iterdir()) == {source, destination}


@pytest.mark.parametrize("command", ["resume-storage", "restore-db"])
def test_activation_final_cleanup_failure_restores_database_fence(
    db, db_engine, tmp_path, monkeypatch, command
):
    from pathlib import Path

    from garmin_ai import cli
    from garmin_ai.models import AppState

    source = tmp_path / "empty.gz"
    export_database(db_engine, source)
    db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data", token_dir=tmp_path / "tokens", lock_dir=tmp_path / "locks"
    )
    settings.lock_dir.mkdir()
    (settings.lock_dir / "erased").write_text("synthetic")
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("garmin_ai.db.make_engine", lambda _: db_engine)
    monkeypatch.setattr(
        "sys.argv", ["garmin-ai", command] + ([str(source)] if command == "restore-db" else [])
    )
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == settings.lock_dir / "activating":
            raise PermissionError("synthetic cleanup")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(SystemExit):
        cli.main()
    db.expire_all()
    assert db.get(AppState, "maintenance:erased") is not None
    assert (settings.lock_dir / "erased").exists()


@pytest.mark.parametrize("command", ["resume-storage", "restore-db"])
@pytest.mark.parametrize("compensation_failure", [None, "file", "database"])
def test_activation_cleanup_flush_failure_compensates_local_fence_first(
    db, db_engine, tmp_path, monkeypatch, command, compensation_failure
):
    from sqlalchemy import event

    from garmin_ai import cli
    from garmin_ai.models import AppState
    from garmin_ai.storage_files import standalone_files

    source = tmp_path / "empty.gz"
    export_database(db_engine, source)
    db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        database_url="",
    )
    settings.lock_dir.mkdir()
    marker = settings.lock_dir / "erased"
    marker.write_text("synthetic")
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("garmin_ai.db.make_engine", lambda _: db_engine)
    monkeypatch.setattr(
        "sys.argv", ["garmin-ai", command] + ([str(source)] if command == "restore-db" else [])
    )
    original_sync = cli.fsync_directory
    original_write = cli.atomic_private_write

    def sync(path):
        if not (settings.lock_dir / "activating").exists():
            raise OSError("synthetic final cleanup flush failure")
        original_sync(path)

    def write(path, *args, **kwargs):
        if path == marker and compensation_failure == "file":
            raise OSError("synthetic local fence write failure")
        original_write(path, *args, **kwargs)

    commits = []

    def committing(connection):
        commits.append(marker.exists())
        if len(commits) == 2:
            assert marker.exists(), "Database compensation requires the local fence"
            if compensation_failure == "database":
                raise OSError("synthetic database compensation failure")

    monkeypatch.setattr(cli, "fsync_directory", sync)
    monkeypatch.setattr(cli, "atomic_private_write", write)
    event.listen(db_engine, "commit", committing)
    try:
        with pytest.raises(SystemExit):
            cli.main()
    finally:
        event.remove(db_engine, "commit", committing)
    db.expire_all()
    assert not (settings.lock_dir / "activating").exists()
    if compensation_failure == "file":
        assert commits == [False]
        assert not marker.exists()
        assert db.get(AppState, "maintenance:erased") is None
    else:
        assert commits == [False, True]
        assert marker.exists()
        assert (db.get(AppState, "maintenance:erased") is not None) == (
            compensation_failure is None
        )
        with pytest.raises(ValueError):
            with standalone_files(settings):
                pytest.fail("The local fence must block commands without database settings")


def test_sync_waits_for_running_backup(db):
    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    backup = enqueue(db, "backup", {}, "backup:synthetic", now)
    assert claim(db, now=now, kinds=["backup"]).id == backup
    sync = enqueue(db, "garmin_activities", {}, "sync:synthetic", now)
    assert claim(db, now=now, kinds=["garmin_activities"]) is None
    db.get(Job, backup).status = "done"
    db.flush()
    assert claim(db, now=now, kinds=["garmin_activities"]).id == sync


def test_backup_dispatch_retains_scheduled_date():
    from datetime import date
    from types import SimpleNamespace

    from garmin_ai.runtime import backup_job_date

    for payload in ({"date": "2026-09-08"}, {}):
        job = SimpleNamespace(
            payload=payload, dedup_key="backup:2026-09-08", run_at=datetime(2026, 9, 9, tzinfo=UTC)
        )
        assert backup_job_date(job) == date(2026, 9, 8)


def test_default_lock_directory_stable_across_working_directories(tmp_path, monkeypatch):
    monkeypatch.delenv("GA_LOCK_DIR", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    a = Settings(
        _env_file=None, data_dir=tmp_path / "private-data", token_dir=tmp_path / "private-tokens"
    )
    monkeypatch.chdir(second)
    b = Settings(
        _env_file=None, data_dir=tmp_path / "private-data", token_dir=tmp_path / "private-tokens"
    )
    assert a.lock_dir == b.lock_dir and a.lock_dir.is_absolute()
    with pytest.raises(ValueError, match="GA_LOCK_DIR must be absolute"):
        Settings(_env_file=None, data_dir=tmp_path / "private-data", lock_dir=".state")


def test_erasure_rejects_directory_junction(db, db_engine, tmp_path, monkeypatch):
    from pathlib import Path

    from garmin_ai.operations import erase_all

    settings = Settings(
        data_dir=tmp_path / "data", token_dir=tmp_path / "tokens", lock_dir=tmp_path / "locks"
    )
    settings.data_dir.mkdir()
    secret = settings.data_dir / "synthetic"
    secret.write_text("preserve")
    monkeypatch.setattr(Path, "is_junction", lambda p: p == settings.data_dir)
    with pytest.raises(ValueError, match="Unsafe erasure"):
        erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
    assert secret.read_text() == "preserve"
    assert not (settings.lock_dir / "erased").exists()


def test_windows_lock_rejects_reparse_handle_before_writing(tmp_path, monkeypatch):
    import ctypes
    import sys
    from ctypes import wintypes
    from types import SimpleNamespace

    from garmin_ai.storage_files import open_windows_lock

    calls = []

    class Function:
        def __init__(self, call):
            self.call = call

        def __call__(self, *args):
            return self.call(*args)

    def create(*args):
        assert args[5] & 0x200000
        return 123

    def info(handle, buffer):
        ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = 0x400
        return 1

    kernel = SimpleNamespace(
        CreateFileW=Function(create),
        GetFileInformationByHandle=Function(info),
        CloseHandle=Function(lambda h: calls.append(h)),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            open_osfhandle=lambda *a: pytest.fail("reparse handle must not be opened for writing")
        ),
    )
    with pytest.raises(ValueError, match="reparse"):
        open_windows_lock(tmp_path / "storage.lock")
    assert calls == [123]


@pytest.mark.parametrize("operation", ["destination", "lock"])
def test_new_directory_ancestors_are_durable_before_use(tmp_path, monkeypatch, operation):
    from garmin_ai import archive
    from garmin_ai.operations import ensure_parent
    from garmin_ai.storage_files import exclusive_files

    destination = tmp_path / "new-parent" / "nested"
    flushed = []

    def flush(path):
        assert path.is_dir()
        flushed.append(path)

    monkeypatch.setattr(archive, "fsync_directory", flush)
    if operation == "destination":
        ensure_parent(destination)
    else:
        with exclusive_files(Settings(lock_dir=destination)):
            assert tmp_path in flushed and destination.parent in flushed
    assert flushed.index(tmp_path) < flushed.index(destination.parent)
    assert destination.is_dir()


def test_directory_flush_failure_is_retried_before_creating_children(tmp_path, monkeypatch):
    from garmin_ai import archive

    destination = tmp_path / "new-parent" / "nested"
    flushed = []

    def flush(path):
        flushed.append(path)
        if path == tmp_path:
            raise OSError("synthetic directory flush failure")

    monkeypatch.setattr(archive, "fsync_directory", flush)
    with pytest.raises(OSError, match="synthetic"):
        archive.durable_directory(destination)
    assert destination.parent.is_dir() and not destination.exists()
    flushed.clear()
    monkeypatch.setattr(archive, "fsync_directory", flushed.append)
    archive.durable_directory(destination)
    assert flushed == [tmp_path, destination.parent]


@pytest.mark.parametrize("source", ["data", "raw", "tokens"])
def test_backup_refuses_junction_source_roots_before_staging(tmp_path, monkeypatch, source):
    from pathlib import Path

    settings = Settings(data_dir=tmp_path / "data", token_dir=tmp_path / "tokens")
    junction = {
        "data": settings.data_dir,
        "raw": settings.data_dir / "raw",
        "tokens": settings.token_dir,
    }[source]
    junction.mkdir(parents=True)
    monkeypatch.setattr(Path, "is_junction", lambda path: path == junction)
    with pytest.raises(ValueError, match="junction"):
        create_backup(None, settings, tmp_path / "backup.enc")
    assert not (settings.data_dir / "backup-work").exists()
    assert not (tmp_path / "backup.enc").exists()


def test_recovered_snapshot_flushes_before_retention_and_retries_failure(tmp_path, monkeypatch):
    import stat

    from garmin_ai import operations

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        backup_dir=tmp_path / "backups",
        backup_key=base64.urlsafe_b64encode(os.urandom(32)).decode(),
    )
    source = tmp_path / "synthetic.tar"
    source.write_bytes(b"synthetic snapshot")
    target = settings.backup_dir / "garmin-ai-2026-09-08.enc"
    encrypt_file(source, target, operations.backup_key(settings))
    flushed = []
    original = os.fsync

    def sync_file(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            flushed.append("file")
        original(descriptor)

    def sync_directory(path):
        if path == settings.data_dir / "backup-work" / ".garmin-ai-plaintext":
            flushed.append("workspace")
            return
        if path == settings.data_dir / "backup-work":
            flushed.append("staging")
            return
        assert path == target.parent
        flushed.append("directory")
        raise OSError("synthetic publication flush failure")

    monkeypatch.setattr(operations.os, "fsync", sync_file)
    monkeypatch.setattr(operations, "fsync_directory", sync_directory)
    monkeypatch.setattr(
        operations, "prune_scheduled_backups", lambda *a, **k: flushed.append("prune")
    )
    with pytest.raises(OSError, match="synthetic"):
        operations.scheduled_backup(None, settings, target)
    assert flushed == ["workspace", "workspace", "staging", "file", "directory"]
    flushed.clear()
    monkeypatch.setattr(
        operations,
        "fsync_directory",
        lambda path: flushed.append(
            "directory"
            if path == target.parent
            else "workspace"
            if path.name == ".garmin-ai-plaintext"
            else "staging"
        ),
    )
    operations.scheduled_backup(None, settings, target)
    assert flushed == ["workspace", "workspace", "staging", "file", "directory", "prune"]


@pytest.mark.parametrize("erase_during_upload", [False, True])
def test_webhook_upload_does_not_hold_maintenance_lock(db, db_engine, erase_during_upload):
    import httpx

    from garmin_ai.api import create_app
    from garmin_ai.models import TelegramUpdate

    secret = "synthetic-webhook-secret-32-characters"
    settings = Settings(telegram_webhook_secret=secret, telegram_user_id=42)
    app = create_app(settings, db_engine)
    update = {
        "update_id": 7001,
        "message": {
            "message_id": 7001,
            "date": int(datetime.now(UTC).timestamp()),
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "/status",
        },
    }

    async def scenario():
        uploading, release = asyncio.Event(), asyncio.Event()

        async def body():
            encoded = json.dumps(update).encode()
            yield encoded[:1]
            uploading.set()
            await release.wait()
            yield encoded[1:]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/telegram/webhook",
                    content=body(),
                    headers={"X-Telegram-Bot-Api-Secret-Token": secret},
                )
            )
            try:
                await asyncio.wait_for(uploading.wait(), 3)
                with db_engine.begin() as conn:
                    assert conn.scalar(text("SELECT pg_try_advisory_xact_lock(72104622)"))
                    if erase_during_upload:
                        conn.execute(
                            text(
                                "INSERT INTO app_state (key, value) VALUES ('maintenance:erased', '{}'::jsonb)"
                            )
                        )
            finally:
                release.set()
                response = await asyncio.wait_for(request, 3)
            assert response.status_code == (503 if erase_during_upload else 200)
            assert (db.get(TelegramUpdate, 7001) is None) == erase_during_upload

    asyncio.run(scenario())


@pytest.mark.parametrize("relative", ["data/raw", "data/nested/raw", "tokens/nested"])
@pytest.mark.parametrize("redirect", ["junction", "symlink"])
def test_erasure_rejects_nested_redirects_before_database_changes(
    db, db_engine, tmp_path, monkeypatch, relative, redirect
):
    from pathlib import Path

    from garmin_ai.models import AppState
    from garmin_ai.operations import erase_all

    settings = Settings(
        data_dir=tmp_path / "data", token_dir=tmp_path / "tokens", lock_dir=tmp_path / "locks"
    )
    record = create_event(
        db,
        EventInput(
            start="2026-09-07T12:00:00Z", payload={"type": "note", "description": "synthetic"}
        ),
        actor="test",
    )
    identity = record.id
    db.commit()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "synthetic.txt").write_text("preserve")
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if redirect == "junction":
        target.mkdir()
        monkeypatch.setattr(Path, "is_junction", lambda path: path == target)
    else:
        target.symlink_to(outside, target_is_directory=True)
    original_scandir = os.scandir

    def scan(path):
        assert Path(path) not in {target, outside}, "must not traverse redirected target"
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", scan)
    with pytest.raises(ValueError, match="nested symlink or junction"):
        erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
    db.expire_all()
    assert db.get(Event, identity) is not None
    assert db.get(AppState, "maintenance:erased") is None
    assert not (settings.lock_dir / "erased").exists()
    assert (outside / "synthetic.txt").read_text() == "preserve"


def test_retention_flushes_deletions_and_retries_failed_directory_flush(tmp_path, monkeypatch):
    from garmin_ai import operations

    old, retained = [tmp_path / f"garmin-ai-2026-09-{day:02d}.enc" for day in (1, 2)]
    old.write_bytes(b"synthetic old")
    retained.write_bytes(b"synthetic current")
    flushed = []

    def flush(path):
        assert path == tmp_path and not old.exists() and retained.exists()
        flushed.append(path)
        if len(flushed) == 1:
            raise OSError("synthetic retention flush failure")

    monkeypatch.setattr(operations, "fsync_directory", flush)
    with pytest.raises(OSError, match="retention flush"):
        operations.prune_scheduled_backups(tmp_path, 1)
    operations.prune_scheduled_backups(tmp_path, 1)
    assert flushed == [tmp_path, tmp_path]
    assert list(tmp_path.iterdir()) == [retained]


@pytest.mark.parametrize("legacy", [False, True])
def test_backup_bounds_wait_for_replenished_sync_retries(db, legacy):
    from datetime import timedelta

    from garmin_ai.jobs import claim, enqueue, finish
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    identity = enqueue(db, "backup", {}, "backup:outage", now)
    if legacy:
        db.get(Job, identity).payload = {}
    for batch in range(4):
        sync = enqueue(
            db, "garmin_activities", {}, f"sync:{batch}", now + timedelta(minutes=15 * batch)
        )
        row = db.get(Job, sync)
        row.attempts, row.last_error = 4, "AuthenticationRequired"
    assert claim(db, now=now + timedelta(minutes=29), kinds=["backup"]) is None
    later = now + timedelta(minutes=31)
    assert claim(db, now=later, kinds=["garmin_activities"]) is None
    backup = claim(db, now=later, kinds=["backup"])
    assert backup.id == identity
    deadline = backup.payload["sync_wait_until"]
    assert datetime.fromisoformat(deadline) == now + timedelta(minutes=30)
    finish(db, backup.id, backup.lease_token, error_type="OSError")
    retry = claim(db, now=later + timedelta(minutes=1), kinds=["backup"])
    assert retry.id == identity and retry.payload["sync_wait_until"] == deadline
    finish(db, retry.id, retry.lease_token)
    assert claim(db, now=later + timedelta(minutes=1), kinds=["garmin_activities"]) is not None


@pytest.mark.parametrize("expired", [False, True])
def test_overdue_backup_waits_only_for_live_sync_writer(db, expired):
    from datetime import timedelta

    from garmin_ai.jobs import claim, enqueue, finish

    now = datetime.now(UTC)
    identity = enqueue(db, "backup", {}, "backup:waiting", now)
    enqueue(db, "garmin_activities", {}, "sync:running", now)
    sync = claim(
        db, now=now + timedelta(minutes=29), lease_seconds=300, kinds=["garmin_activities"]
    )
    enqueue(db, "garmin_activities", {}, "sync:next", now)
    later = now + timedelta(minutes=31)
    assert claim(db, now=later, kinds=["garmin_activities"]) is None
    if expired:
        sync.lease_until = later - timedelta(seconds=1)
        db.flush()
    else:
        assert claim(db, now=later, kinds=["backup"]) is None
        finish(db, sync.id, sync.lease_token)
    assert claim(db, now=later, kinds=["backup"]).id == identity


@pytest.mark.parametrize("operation", ["create", "recover"])
def test_backup_rejects_junction_plaintext_staging_before_writing(tmp_path, monkeypatch, operation):
    from pathlib import Path

    from garmin_ai.operations import scheduled_backup

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        backup_dir=tmp_path / "backups",
        backup_key=base64.urlsafe_b64encode(os.urandom(32)).decode(),
    )
    staging = settings.data_dir / "backup-work"
    staging.mkdir(parents=True)
    sentinel = staging / "synthetic.txt"
    sentinel.write_text("preserve")
    monkeypatch.setattr(Path, "is_junction", lambda path: path == staging)
    target = settings.backup_dir / "snapshot.enc"
    if operation == "recover":
        target.parent.mkdir()
        target.write_bytes(b"synthetic existing snapshot")
    with pytest.raises(ValueError, match="junction"):
        if operation == "create":
            create_backup(None, settings, target)
        else:
            scheduled_backup(None, settings, target)
    assert list(staging.iterdir()) == [sentinel]
    assert sentinel.read_text() == "preserve"
    if operation == "create":
        assert not target.exists()
    else:
        assert target.read_bytes() == b"synthetic existing snapshot"


@pytest.mark.parametrize("operation", ["export", "encrypt"])
@pytest.mark.parametrize("fail_cleanup_flush", [False, True])
def test_export_persists_plaintext_temporary_unlink(
    db, db_engine, tmp_path, monkeypatch, fail_cleanup_flush, operation
):
    from contextlib import nullcontext

    from garmin_ai import operations

    destination = tmp_path / "export.gz"
    entries_at_flush = []

    def flush(path):
        assert path == tmp_path
        entries_at_flush.append(set(path.iterdir()))
        if len(entries_at_flush) == 2 and fail_cleanup_flush:
            raise OSError("synthetic cleanup flush failure")

    monkeypatch.setattr(operations, "fsync_directory", flush)
    with pytest.raises(OSError, match="cleanup flush") if fail_cleanup_flush else nullcontext():
        if operation == "export":
            export_database(db_engine, destination)
        else:
            source = tmp_path.parent / (tmp_path.name + "-synthetic")
            source.write_bytes(b"synthetic plaintext")
            try:
                encrypt_file(source, destination, os.urandom(32))
            finally:
                source.unlink()
    assert len(entries_at_flush) == 2
    assert destination in entries_at_flush[0] and len(entries_at_flush[0]) == 2
    assert entries_at_flush[1] == {destination}


@pytest.mark.parametrize("storage", ["data", "tokens"])
@pytest.mark.parametrize("redirect", ["symlink", "junction"])
def test_erasure_rejects_redirected_ancestors_before_scanning(
    db, db_engine, tmp_path, monkeypatch, storage, redirect
):
    from pathlib import Path

    from garmin_ai.models import AppState
    from garmin_ai.operations import erase_all

    ancestor = tmp_path / "redirect"
    outside = tmp_path / "outside"
    outside.mkdir()
    if redirect == "symlink":
        ancestor.symlink_to(outside, target_is_directory=True)
    else:
        ancestor.mkdir()
        monkeypatch.setattr(Path, "is_junction", lambda path: path == ancestor)
    root = ancestor / "storage"
    root.mkdir()
    sentinel = root / "synthetic.txt"
    sentinel.write_text("preserve")
    settings = Settings(
        data_dir=root if storage == "data" else tmp_path / "data",
        token_dir=root if storage == "tokens" else tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
    )
    record = create_event(
        db, EventInput(start="2026-09-07T12:00:00Z", payload={"type": "migraine"}), actor="test"
    )
    identity = record.id
    db.commit()
    monkeypatch.setattr(os, "scandir", lambda *args: pytest.fail("must reject before scanning"))
    with pytest.raises(ValueError, match="redirected path component"):
        erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
    db.expire_all()
    assert db.get(Event, identity) is not None
    assert db.get(AppState, "maintenance:erased") is None
    assert not (settings.lock_dir / "erased").exists()
    assert sentinel.read_text() == "preserve"


@pytest.mark.parametrize("status", ["pending", "running"])
def test_runtime_without_backup_key_ignores_orphaned_backup(
    db, db_engine, tmp_path, monkeypatch, status
):
    from datetime import timedelta

    from garmin_ai import runtime
    from garmin_ai.jobs import enqueue
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    backup_id = enqueue(db, "backup", {}, "backup:orphaned", now - timedelta(hours=2))
    row = db.get(Job, backup_id)
    row.status, row.attempts = status, 1
    row.lease_until = now + timedelta(minutes=5) if status == "running" else None
    sync_id = enqueue(db, "garmin_activities", {}, "sync:after-restart", now)
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        llm_enabled=False,
        telegram_bot_token="",
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)
    monkeypatch.setattr(runtime.GarminReader, "restore", lambda path: runtime.GarminReader(None))
    monkeypatch.setattr(runtime, "run_garmin_job", lambda *args: None)

    async def scenario():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "add_signal_handler",
            lambda signum, cb: callbacks.append(cb),
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                db.expire_all()
                if db.get(Job, sync_id).status == "done":
                    break
            assert db.get(Job, sync_id).status == "done"
            assert db.get(Job, backup_id).status == status
        finally:
            db.rollback()
            callbacks[0]()
            await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["create", "recover", "unpack"])
@pytest.mark.parametrize("fail_flush", [False, True])
def test_plaintext_workspaces_are_durably_removed(
    db, db_engine, tmp_path, monkeypatch, operation, fail_flush
):
    from contextlib import nullcontext

    from garmin_ai import operations

    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        backup_dir=tmp_path / "backups",
        backup_key=base64.urlsafe_b64encode(os.urandom(32)).decode(),
    )
    snapshot = settings.backup_dir / "snapshot.enc"
    if operation != "create":
        create_backup(db_engine, settings, snapshot)
    recovery_parent = tmp_path / "recovery"
    recovery_parent.mkdir()
    parent = recovery_parent if operation == "unpack" else settings.data_dir / "backup-work"
    checked = []
    original = operations.fsync_directory

    def flush(path):
        # Publication also flushes recovery_parent before workspace cleanup.
        if path == parent and not (parent / ".garmin-ai-plaintext" / "active").exists():
            checked.append(path)
            if fail_flush:
                raise OSError("synthetic plaintext cleanup flush failure")
        original(path)

    monkeypatch.setattr(operations, "fsync_directory", flush)
    with pytest.raises(OSError, match="plaintext cleanup") if fail_flush else nullcontext():
        if operation == "create":
            create_backup(db_engine, settings, snapshot)
        elif operation == "recover":
            operations.scheduled_backup(db_engine, settings, snapshot)
        else:
            unpack_backup(settings, snapshot, recovery_parent / "restored")
    assert checked == [parent]
    assert not (parent / ".garmin-ai-plaintext" / "active").exists()


@pytest.mark.parametrize("operation", ["backup_data", "backup_tokens", "lock"])
@pytest.mark.parametrize("redirect", ["symlink", "junction"])
def test_backup_and_lock_reject_redirected_ancestors(tmp_path, monkeypatch, operation, redirect):
    from pathlib import Path

    from garmin_ai.storage_files import standalone_files

    outside = tmp_path / "outside"
    outside.mkdir()
    ancestor = tmp_path / "redirect"
    if redirect == "symlink":
        ancestor.symlink_to(outside, target_is_directory=True)
    else:
        ancestor.mkdir()
        monkeypatch.setattr(Path, "is_junction", lambda path: path == ancestor)
    root = ancestor / "storage"
    root.mkdir()
    sentinel = root / "synthetic.txt"
    sentinel.write_text("preserve")
    settings = Settings(
        data_dir=root if operation == "backup_data" else tmp_path / "data",
        token_dir=root if operation == "backup_tokens" else tmp_path / "tokens",
        lock_dir=root if operation == "lock" else tmp_path / "locks",
        database_url="",
    )
    with pytest.raises(ValueError, match="redirected ancestor"):
        if operation == "lock":
            with standalone_files(settings):
                pytest.fail("must not enter redirected lock directory")
        else:
            create_backup(None, settings, tmp_path / "snapshot.enc")
    assert list(root.iterdir()) == [sentinel]
    assert sentinel.read_text() == "preserve"
    assert not (tmp_path / "snapshot.enc").exists()


def test_erasure_rejects_canonical_home_when_home_is_symlink(db, db_engine, tmp_path, monkeypatch):
    from pathlib import Path

    from garmin_ai.models import AppState
    from garmin_ai.operations import erase_all

    canonical_home = tmp_path / "deep" / "users" / "home"
    canonical_home.mkdir(parents=True)
    sentinel = canonical_home / "synthetic.txt"
    sentinel.write_text("preserve")
    home_link = tmp_path / "home-link"
    home_link.symlink_to(canonical_home, target_is_directory=True)
    settings = Settings(
        data_dir=canonical_home, token_dir=tmp_path / "tokens", lock_dir=tmp_path / "locks"
    )
    record = create_event(
        db, EventInput(start="2026-09-07T12:00:00Z", payload={"type": "migraine"}), actor="test"
    )
    identity = record.id
    db.commit()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home_link))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Unsafe erasure"):
        erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
    db.expire_all()
    assert db.get(Event, identity) is not None
    assert db.get(AppState, "maintenance:erased") is None
    assert not (settings.lock_dir / "erased").exists()
    assert sentinel.read_text() == "preserve"


@pytest.mark.parametrize("race", [False, True])
def test_windows_invalid_function_uses_exclusive_rename(tmp_path, monkeypatch, race):
    import errno

    from garmin_ai import operations

    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"synthetic payload")
    original_link = os.link
    calls = []

    def unsupported(*args):
        if race:
            destination.write_bytes(b"preserve")
        raise OSError(errno.EINVAL, "synthetic Windows ERROR_INVALID_FUNCTION")

    def windows_rename(src, dst):
        calls.append((src, dst))
        # Model Windows no-replace rename semantics on the Linux test filesystem.
        original_link(src, dst)
        src.unlink()

    with monkeypatch.context() as patch:
        patch.setattr(operations.sys, "platform", "win32")
        patch.setattr(operations.os, "link", unsupported)
        patch.setattr(operations.os, "rename", windows_rename)
        if race:
            with pytest.raises(FileExistsError):
                operations.publish_file(source, destination)
        else:
            operations.publish_file(source, destination)
    assert calls == [(source, destination)]
    assert destination.read_bytes() == (b"preserve" if race else b"synthetic payload")
    assert source.exists() == race


@pytest.mark.parametrize(
    "platform,code", [("linux", "EINVAL"), ("win32", "EACCES"), ("win32", "EEXIST")]
)
def test_publication_does_not_fallback_for_unrelated_link_errors(
    tmp_path, monkeypatch, platform, code
):
    import errno

    from garmin_ai import operations

    def fail(*args):
        raise OSError(getattr(errno, code), "synthetic link error")

    with monkeypatch.context() as patch:
        patch.setattr(operations.sys, "platform", platform)
        patch.setattr(operations.os, "link", fail)
        patch.setattr(
            operations, "publish_directory", lambda *args: pytest.fail("unexpected fallback")
        )
        with pytest.raises(OSError) as error:
            operations.publish_file(tmp_path / "source", tmp_path / "destination")
    assert error.value.errno == getattr(errno, code)
