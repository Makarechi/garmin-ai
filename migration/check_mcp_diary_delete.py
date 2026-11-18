"""Compare audited deletion through the pinned Python and native MCP servers."""

import argparse
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from capture_reference import isolated_engine
from check_mcp_diary_create import cancel_write, create, delay_write, open_client
from check_mcp_stdio import PYTHON_SERVER, document
from sqlalchemy import text


async def delete(client, event, revision, **extra):
    return document(
        await client.call_tool("events_delete", {"event_id": event, "revision": revision, **extra})
    )


def audits(engine, event):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT action, actor, before, after FROM audit_log WHERE event_id=:id ORDER BY id"
            ),
            {"id": UUID(event)},
        ).all()


def permissions(engine, event, *allowed):
    version = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO event_definition_versions
                    (id,definition_id,version,schema,schema_hash,topology,
                     field_metadata,labels,privacy,allowed_operations)
                SELECT :version, definition_id, version + 1, schema, :hash, topology,
                       field_metadata, labels, privacy, to_jsonb(CAST(:ops AS text[]))
                FROM event_definition_versions
                WHERE id=(SELECT definition_version_id FROM events WHERE id=:id)
            """),
            {"id": UUID(event), "ops": list(allowed), "version": version, "hash": str(version)},
        )
        connection.execute(
            text("UPDATE events SET definition_version_id=:version WHERE id=:id"),
            {"id": UUID(event), "version": version},
        )


async def verify(binary, root, engine):
    env = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    event = "00000000-0000-0000-0000-000000000001"
    async with open_client(str(binary), ["mcp"], root, env, "read-only") as native:
        assert "events_delete" not in [tool.name for tool in (await native.list_tools()).tools]
        failure = await delete(native, event, 1)
        assert failure["isError"] and "PermissionError" in failure["content"][0]["text"]

    env["GA_MCP_ENABLE_WRITES"] = "true"
    async with (
        open_client(sys.executable, ["-c", PYTHON_SERVER], root, env, "python") as python,
        open_client(str(binary), ["mcp"], root, env, "native") as native,
    ):
        descriptors = [
            {item.name: document(item) for item in (await client.list_tools()).tools}
            for client in (python, native)
        ]
        assert all("events_delete" in tools for tools in descriptors), [
            list(tools) for tools in descriptors
        ]
        assert descriptors[0]["events_delete"] == descriptors[1]["events_delete"]
        created = await create(python, "fixture-delete")
        entry = created["structuredContent"]
        assert not created["isError"] and entry["revision"] == 1
        cases = [
            (event, 1, {}),
            ("invalid", 1, {}),
            (entry["id"], 0, {}),
            (entry["id"], 1.5, {}),
            (entry["id"], 2, {}),
            (entry["id"], 2**40, {}),
            (entry["id"], 1, {"unexpected": True}),
        ]
        for event_id, revision, extra in cases:
            left = await delete(python, event_id, revision, **extra)
            right = await delete(native, event_id, revision, **extra)
            assert left == right and right["isError"], (event_id, revision, left, right)
        assert len(audits(engine, entry["id"])) == 1

        with delay_write(engine, "UPDATE"):
            await cancel_write(
                binary,
                engine,
                root,
                env,
                "events_delete",
                {"event_id": entry["id"], "revision": 1},
            )
        assert len(audits(engine, entry["id"])) == 1
        result = await delete(native, entry["id"], "1.0")
        assert not result["isError"]
        deleted = result["structuredContent"]
        assert deleted["deleted"] and deleted["revision"] == 2
        assert deleted["payload"] == entry["payload"]
        assert deleted["ingested_at"] == entry["ingested_at"]
        stored = audits(engine, entry["id"])
        assert len(stored) == 2
        assert stored[-1] == ("delete", "mcp", entry, deleted)
        for client in (python, native):
            failure = await delete(client, entry["id"], 2)
            assert failure["isError"] and "LookupError" in failure["content"][0]["text"]

        hidden = (await create(native, "fixture-hidden"))["structuredContent"]
        permissions(engine, hidden["id"], "query")
        left = await delete(python, hidden["id"], 1)
        right = await delete(native, hidden["id"], 1)
        assert left == right and right["isError"]
        assert len(audits(engine, hidden["id"])) == 1
        permissions(engine, hidden["id"], "delete")
        safe = await delete(native, hidden["id"], 1)
        assert safe["structuredContent"] == {"id": hidden["id"], "revision": 2, "deleted": True}
        assert audits(engine, hidden["id"])[-1].before["payload"] == hidden["payload"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            asyncio.run(verify(args.binary.resolve(strict=True), Path(directory), engine))
    finally:
        engine.dispose()
    print("Verified MCP deletion descriptors, revision conflicts, cancellation, and audit history")


if __name__ == "__main__":
    main()
