"""Use the pinned MCP client to compare model-scoped diary reads with Python."""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_model_diary_policy import END, START, TRACKER, seed
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text

PYTHON_SERVER = """
import asyncio
from mcp.server.stdio import stdio_server
from garmin_ai.config import Settings
from garmin_ai.db import make_engine
from garmin_ai.mcp_server import build_server

async def serve():
    settings = Settings(_env_file=None)
    engine = make_engine(settings)
    try:
        server = build_server(engine, timezone="UTC", enable_writes=settings.mcp_enable_writes)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        engine.dispose()

asyncio.run(serve())
"""


def grant(engine):
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO app_state(key,value)
            SELECT :target,value || '{"destination_instance_id":"model:mcp:local"}'::jsonb
            FROM app_state WHERE key=:source
            """),
            {
                "source": f"tracker-consent:{TRACKER}:model:model:primary",
                "target": f"tracker-consent:{TRACKER}:model:model:mcp:local",
            },
        )


def document(obj):
    return obj.model_dump(by_alias=True, exclude_none=True)


async def session(command, arguments, root, env, *, native):
    process = StdioServerParameters(command=command, args=arguments, cwd=root, env=env)
    with (root / f"{'native' if native else 'python'}.log").open("w", encoding="utf-8") as log:
        async with (
            stdio_client(process, errlog=log) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            initialized = document(await client.initialize())
            assert initialized["protocolVersion"] == "2025-11-25"
            listed = (await client.list_tools()).tools
            if native:
                assert all(tool.annotations.readOnlyHint for tool in listed)
            event = next(tool for tool in listed if tool.name == "events")
            assert event.annotations.readOnlyHint and event.annotations.openWorldHint is False
            cases = []
            args = {"start": START.isoformat(), "end": END.isoformat()}
            for name, values in (
                ("events", args),
                ("events", {**args, "kind": "user.tracker"}),
                ("events", {**args, "unknown": "arbitrary argument"}),
                ("events", {**args, "start": END.isoformat(), "end": START.isoformat()}),
                (
                    "events_delete",
                    {"event_id": "00000000-0000-0000-0000-000000000004", "revision": 1},
                ),
                ("unknown_tool", {}),
            ):
                response = document(await client.call_tool(name, values))
                if not response["isError"]:
                    assert (
                        json.loads(response["content"][0]["text"]) == response["structuredContent"]
                    )
                    assert "synthetic quote" not in json.dumps(response)
                    response["content"][0]["text"] = response["structuredContent"]
                cases.append(response)
    return initialized, document(event), cases


async def compare(binary, root, engine):
    env = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_MCP_ENABLE_WRITES": "false",
    }
    for revoked in (False, True):
        if revoked:
            with engine.begin() as connection:
                connection.execute(
                    text("""UPDATE app_state SET value='{"model_categories":["health"]}'
                          WHERE key='preferences:onboarding'""")
                )
        expected = await session(sys.executable, ["-c", PYTHON_SERVER], root, env, native=False)
        actual = await session(str(binary), ["mcp"], root, env, native=True)
        for label, reference, candidate in zip(
            ("initialization", "tool", "calls"), expected, actual, strict=True
        ):
            assert candidate == reference, label
        if not revoked:
            assert len(actual[2][0]["structuredContent"]["rows"]) == 3
        else:
            assert actual[2][0]["isError"]
    with engine.connect() as connection:
        unchanged = connection.scalar(text("SELECT count(*) FROM events WHERE NOT deleted"))
        assert unchanged == 1204
    await check_frame_limit(binary, root, env)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO app_state(key,value) VALUES ('maintenance:erased','{}')")
        )
    erased = subprocess.run(
        [binary, "mcp"],
        cwd=root,
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (
        erased.returncode == 1
        and not erased.stdout
        and "MCP storage is unavailable" in erased.stderr
    )


async def check_frame_limit(binary, root, env):
    process = await asyncio.create_subprocess_exec(
        binary,
        "mcp",
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
        assert (await read())["id"] == 1
        await write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await write(
            {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {"padding": "x" * 1_048_577}}
        )
        await write({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        response = await read()
        if "error" in response:
            assert response.get("id") is None
            response = await read()
        assert response == {"jsonrpc": "2.0", "id": 3, "result": {}}
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), timeout=5) == 0
        assert not await process.stdout.read()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed(engine)
        grant(engine)
        with TemporaryDirectory() as directory:
            asyncio.run(compare(args.binary.resolve(strict=True), Path(directory), engine))
    finally:
        engine.dispose()
    print(
        "Verified MCP initialization, the diary tool, and twelve pinned Python read and denial cases"
    )


if __name__ == "__main__":
    main()
