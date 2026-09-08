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

    settings = Settings(data_dir=tmp_path / "data", token_dir=tmp_path / "tokens")
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

    settings = Settings(data_dir=tmp_path / "data", token_dir=tmp_path / "tokens")
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
