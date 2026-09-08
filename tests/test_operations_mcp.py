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
    assert calls == ["file", "publish", "directory"]


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
    assert calls == ["file", "publish", "directory"]


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
        server = mcp_server.build_server(db_engine)
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
def test_activation_commit_failure_restores_local_fence(
    db, db_engine, tmp_path, monkeypatch, command
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

    def fail_commit(connection):
        assert not marker.exists()
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
