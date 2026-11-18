"""Compare pinned MCP initialization versions and basic lifecycle messages."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from capture_reference import isolated_engine
from check_mcp_stdio import PYTHON_SERVER

VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2099-01-01")


async def capture(command, arguments, version, environment):
    process = await asyncio.create_subprocess_exec(
        command,
        *arguments,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def send(document):
        process.stdin.write(json.dumps(document).encode() + b"\n")
        await process.stdin.drain()

    async def receive():
        wire = await asyncio.wait_for(process.stdout.readline(), 10)
        assert wire, "MCP closed before completing the lifecycle"
        return json.loads(wire)

    try:
        await send({"jsonrpc": "2.0", "id": "before-initialize", "method": "ping"})
        first = await receive()
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-protocol", "version": "1"},
                },
            }
        )
        initialized = await receive()
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": "synthetic 🧪", "method": "ping"})
        ping = await receive()
        await send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
            }
        )
        unknown = await receive()
        await send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": "already-finished"},
            }
        )
        await send({"jsonrpc": "2.0", "id": 4, "method": "ping"})
        resumed = await receive()
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 5) == 0
        assert not await process.stdout.read()
        return first, initialized, ping, unknown, resumed
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def compare(binary, engine):
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_MCP_ENABLE_WRITES": "false",
    }
    for version in VERSIONS:
        expected = await capture(sys.executable, ["-c", PYTHON_SERVER], version, environment)
        actual = await capture(str(binary), ["mcp"], version, environment)
        assert actual == expected, (version, expected, actual)
        negotiated = version if version in VERSIONS[:-1] else "2025-11-25"
        assert actual[1]["result"]["protocolVersion"] == negotiated
    print("Matched four MCP protocol revisions, fallback negotiation, pings, error replies and EOF")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        asyncio.run(compare(args.binary.resolve(strict=True), engine))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
