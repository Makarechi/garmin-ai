"""Exercise MCP cancellation and replay after an audited write has committed."""

import argparse
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_custom_entries import configure_metric, entry, specification
from check_mcp_custom_diary import record
from check_mcp_diary_create import (
    REPORT,
    begin_write,
    create,
    notify_cancel,
    open_client,
    open_wire,
)
from check_mcp_diary_delete import audits, delete
from check_mcp_diary_update import update
from check_mcp_stdio import PYTHON_SERVER, document
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.definitions import activate_definition, create_definition_draft

RANGE = {"start": "2026-10-25T00:00:00Z", "end": "2026-10-26T00:00:00Z"}


def created_id(engine, key):
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT e.id::text FROM events e JOIN audit_log a ON a.event_id=e.id "
                "WHERE e.idempotency_key=:key AND a.actor='mcp' AND a.action='create'"
            ),
            {"key": key},
        )


def revision_written(engine, event, action):
    history = audits(engine, event)
    if history and history[-1].action == action:
        return history[-1].after["revision"]
    return None


async def discard_after_commit(server, root, env, tool, arguments, observed):
    async with open_wire(*server, root, env) as (write, _read):
        await begin_write(write, tool, arguments)
        async with asyncio.timeout(5):
            while (result := await asyncio.to_thread(observed)) is None:
                await asyncio.sleep(0.02)
        await notify_cancel(write)
        return result


async def visible(client):
    reply = document(await client.call_tool("events", RANGE))
    assert not reply["isError"]
    return {row["id"]: row for row in reply["structuredContent"]["rows"]}


async def exercise(root, engine, env, writer, recovery, label):
    key = f"fixture-commit-recovery-{label}"
    event_id = await discard_after_commit(
        writer,
        root,
        env,
        "events_create",
        {"event": REPORT, "idempotency_key": key},
        lambda: created_id(engine, key),
    )
    async with open_client(*recovery, root, env, label + "-create") as client:
        replayed = await create(client, key)
        assert not replayed["isError"] and replayed["structuredContent"]["id"] == event_id
        assert (await visible(client))[event_id]["revision"] == 1
    assert len(audits(engine, event_id)) == 1

    corrected = f"Synthetic {label} correction after an unread reply"
    changes = {"payload": {"description": corrected}}
    assert (
        await discard_after_commit(
            writer,
            root,
            env,
            "events_update",
            {"event_id": event_id, "revision": 1, "changes": changes},
            lambda: revision_written(engine, event_id, "update"),
        )
        == 2
    )
    async with open_client(*recovery, root, env, label + "-update") as client:
        response = await update(client, event_id, 1, changes)
        assert response["isError"] and "Conflict" in response["content"][0]["text"]
        current = (await visible(client))[event_id]
        assert current["revision"] == 2 and current["payload"]["description"] == corrected
    assert [row.action for row in audits(engine, event_id)] == ["create", "update"]

    assert (
        await discard_after_commit(
            writer,
            root,
            env,
            "events_delete",
            {"event_id": event_id, "revision": 2},
            lambda: revision_written(engine, event_id, "delete"),
        )
        == 3
    )
    async with open_client(*recovery, root, env, label + "-delete") as client:
        response = await delete(client, event_id, 2)
        assert response["isError"] and "LookupError" in response["content"][0]["text"]
        assert event_id not in await visible(client)
    history = audits(engine, event_id)
    assert [row.action for row in history] == ["create", "update", "delete"]
    assert history[-1].after["deleted"] and history[-1].after["revision"] == 3

    key = f"fixture-commit-recovery-custom-{label}"
    report = entry(3, timezone="Europe/Bratislava")
    custom_id = await discard_after_commit(
        writer,
        root,
        env,
        "entries_create",
        {"entry": report, "idempotency_key": key},
        lambda: created_id(engine, key),
    )
    async with open_client(*recovery, root, env, label + "-custom") as client:
        replayed = await record(client, key, report)
        assert not replayed["isError"] and replayed["structuredContent"]["id"] == custom_id
    assert len(audits(engine, custom_id)) == 1
    print(f"{label} writes recovered after unread responses for all four mutation tools")


async def verify(binary, root, engine):
    with Session(engine) as session, session.begin():
        draft = create_definition_draft(
            session, specification(10, True), actor="synthetic", authorized=True
        )
        definition = activate_definition(
            session, draft.id, draft.revision, actor="synthetic", authorized=True
        )
        configure_metric(session, definition, 10)
    env = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_MCP_ENABLE_WRITES": "true",
    }
    native = (str(binary), ["mcp"])
    python = (sys.executable, ["-c", PYTHON_SERVER])
    await exercise(root, engine, env, native, python, "native")
    await exercise(root, engine, env, python, native, "frozen-python")


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


if __name__ == "__main__":
    main()
