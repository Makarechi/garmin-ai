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
    backup = tmp_path / "backup.enc"
    create_backup(db_engine, settings, backup)
    unpack_backup(settings, backup, tmp_path / "unpacked")
    assert (tmp_path / "unpacked/raw/synthetic.json").read_text() == '{"synthetic": true}'
    assert backup.stat().st_mode & 0o777 == 0o600


def test_mcp_stdio_lists_and_executes_bounded_tools(db_engine):
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
            invalid = await client.call_tool("metric_series", {"metric": "invalid"})
            assert invalid.isError

    asyncio.run(check())
