"""Measure stdio MCP startup and repeated read-list round trips."""

import argparse
import asyncio
import json
import os
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(idle_seconds):
    params = StdioServerParameters(
        command="garmin-ai",
        args=["mcp"],
        env={
            name: os.environ[name]
            for name in (
                "GA_DATABASE_URL",
                "GA_DATA_DIR",
                "GA_TOKEN_DIR",
                "GA_BACKUP_DIR",
                "GA_LOCK_DIR",
                "OPENBLAS_NUM_THREADS",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
            )
        },
    )
    started = time.monotonic()
    async with (
        asyncio.timeout(35),
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as client,
    ):
        await client.initialize()
        first = await client.list_tools()
        startup = time.monotonic() - started
        if len(first.tools) != 23 or any(not tool.annotations.readOnlyHint for tool in first.tools):
            raise RuntimeError("Unexpected baseline MCP capabilities")
        await asyncio.sleep(idle_seconds)
        start = time.monotonic()
        for _ in range(10):
            listing = await client.list_tools()
            if [tool.name for tool in listing.tools] != [tool.name for tool in first.tools]:
                raise RuntimeError("MCP tools changed after idle")
        return {
            "initialization_and_first_list_seconds": round(startup, 3),
            "idle_seconds": idle_seconds,
            "tool_count": len(first.tools),
            "ten_warm_list_seconds": round(time.monotonic() - start, 3),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("idle_seconds", type=int, choices=(5, 10))
    options = parser.parse_args()
    print(json.dumps(asyncio.run(run(options.idle_seconds))))
