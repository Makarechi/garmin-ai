"""Compare opt-in MCP writes, retry and cancellation on a disposable database."""

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_mcp_stdio import PYTHON_SERVER, document
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text

REPORT = {
    "start": "2026-10-25T10:00:00Z",
    "source": "manual",
    "payload": {"type": "note", "description": "synthetic MCP diary entry", "tags": ["fixture"]},
}


@asynccontextmanager
async def open_client(command, args, root, env, label):
    process = StdioServerParameters(command=command, args=args, cwd=root, env=env)
    with (root / f"{label}.log").open("w", encoding="utf-8") as log:
        async with (
            stdio_client(process, errlog=log) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            await client.initialize()
            yield client


async def create(client, key, event=REPORT):
    return document(
        await client.call_tool("events_create", {"event": event, "idempotency_key": key})
    )


def counts(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("""
            SELECT (SELECT count(*) FROM events),
                   (SELECT count(*) FROM audit_log WHERE actor='mcp' AND action='create'),
                   (SELECT count(*) FROM events WHERE source='mcp' AND transport='mcp')
            """)
        ).one()


def sleeping(engine):
    with engine.connect() as connection:
        return connection.scalar(
            text("""
            SELECT EXISTS(SELECT FROM pg_stat_activity WHERE application_name='garmin-ai-rust'
                 AND datname=current_database() AND wait_event='PgSleep')
            """)
        )


@contextmanager
def delay_write(engine, action):
    assert action in {"INSERT", "UPDATE"}
    with engine.begin() as connection:
        connection.execute(
            text("""
            CREATE FUNCTION wait_for_mcp_fixture() RETURNS trigger AS $$
            BEGIN PERFORM pg_sleep(0.5); RETURN NEW; END; $$ LANGUAGE plpgsql
            """)
        )
        connection.execute(
            text(f"""
            CREATE TRIGGER wait_for_mcp_fixture BEFORE {action} ON events
            FOR EACH ROW EXECUTE FUNCTION wait_for_mcp_fixture()
            """)
        )
    try:
        yield
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER wait_for_mcp_fixture ON events"))
            connection.execute(text("DROP FUNCTION wait_for_mcp_fixture()"))


@asynccontextmanager
async def open_wire(command, args, root, env):
    process = await asyncio.create_subprocess_exec(
        command,
        *args,
        cwd=root,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def write(document):
        process.stdin.write(json.dumps(document).encode() + b"\n")
        await process.stdin.drain()

    async def read():
        return json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=5))

    try:
        await write(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "fixture", "version": "1"},
                },
            }
        )
        result = await read()
        assert result["id"] == 1 and result["result"]["protocolVersion"] == "2025-11-25"
        await write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        yield write, read
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), timeout=5) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def begin_write(write, name, arguments):
    await write(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


async def notify_cancel(write):
    await write({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}})


async def cancel_write(binary, engine, root, env, name, arguments):
    async with open_wire(binary, ["mcp"], root, env) as (write, read):
        await begin_write(write, name, arguments)
        for _ in range(30):
            if await asyncio.to_thread(sleeping, engine):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("The MCP write never reached the cancellable database operation")
        await notify_cancel(write)
        await asyncio.sleep(0.6)
        await write({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        response = await read()
        if response.get("id") == 2:
            assert "error" in response or response["result"]["isError"]
            response = await read()
        assert response["id"] == 3 and response["result"] == {}


async def compare(binary, root, engine):
    env = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    async with open_client(str(binary), ["mcp"], root, env, "read-only") as read_only:
        listed = (await read_only.list_tools()).tools
        assert "events" in [tool.name for tool in listed]
        assert all(tool.annotations.readOnlyHint for tool in listed)
        denial = await create(read_only, "fixture-disabled")
        assert denial["isError"] and "PermissionError" in denial["content"][0]["text"]
    assert counts(engine) == (0, 0, 0)
    env["GA_MCP_ENABLE_WRITES"] = "true"
    async with (
        open_client(sys.executable, ["-c", PYTHON_SERVER], root, env, "python") as python,
        open_client(str(binary), ["mcp"], root, env, "native") as native,
    ):
        left = {tool.name: document(tool) for tool in (await python.list_tools()).tools}
        right = {tool.name: document(tool) for tool in (await native.list_tools()).tools}
        assert right["events_create"] == left["events_create"]
        for payload in (
            {"type": "wellbeing_observation", "energy": 4},
            {"type": "activity_effort", "activity_id": "synthetic", "perceived_exertion": 4},
        ):
            spoofed = {**REPORT, "source": "wearable", "status": "confirmed", "payload": payload}
            rejected = await create(python, "fixture-wearable-spoof", spoofed)
            assert rejected["isError"] and "ValidationError" in rejected["content"][0]["text"]
            assert await create(native, "fixture-wearable-spoof", spoofed) == rejected
        assert counts(engine) == (0, 0, 0)
        source = await create(python, "fixture-python")
        assert not source["isError"]
        target = await create(native, "fixture-python")
        assert not target["isError"] and target["structuredContent"] == source["structuredContent"]
        native_write = await create(native, "fixture-rust")
        assert not native_write["isError"]
        python_retry = await create(python, "fixture-rust")
        assert (
            not python_retry["isError"]
            and python_retry["structuredContent"] == native_write["structuredContent"]
        )
        changed = {**REPORT, "payload": {**REPORT["payload"], "description": "changed fixture"}}
        conflict = await create(native, "fixture-rust", changed)
        assert conflict["isError"]
    assert counts(engine) == (2, 2, 2)
    with delay_write(engine, "INSERT"):
        await cancel_write(
            binary,
            engine,
            root,
            env,
            "events_create",
            {"event": REPORT, "idempotency_key": "fixture-cancelled"},
        )
    assert counts(engine) == (2, 2, 2)
    async with open_client(str(binary), ["mcp"], root, env, "retry") as native:
        resumed = await create(native, "fixture-cancelled")
        assert not resumed["isError"]
    assert counts(engine) == (3, 3, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            asyncio.run(compare(args.binary.resolve(strict=True), Path(directory), engine))
    finally:
        engine.dispose()
    print(
        "Matched MCP create descriptors, bidirectional retries, and rollback on request cancellation"
    )


if __name__ == "__main__":
    main()
