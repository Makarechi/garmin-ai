"""Measure stdio MCP startup and repeated read-list round trips."""

import argparse
import asyncio
import json
import os
import time
from importlib.metadata import version

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(idle_seconds, *, container=None, native=False):
    if container:
        executable = "garmin-ai-rust" if native else "garmin-ai"
        params = StdioServerParameters(
            command="docker",
            args=["exec", "-i", container, executable, "mcp"],
            env={
                key: os.environ[key]
                for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
                if key in os.environ
            },
        )
    else:
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
        report = {
            "initialization_and_first_list_seconds": round(startup, 3),
            "idle_seconds": idle_seconds,
            "tool_count": len(first.tools),
            "ten_warm_list_seconds": round(time.monotonic() - start, 3),
        }
        if container:
            start = time.monotonic()
            response = await client.call_tool(
                "resource_catalog", {"kind": "raw_sources", "resource_key": "source.raw_only"}
            )
            if response.isError or response.structuredContent is None:
                raise RuntimeError("The MCP resource read failed after idle")
            if response.structuredContent != {"resources": [], "next_cursor": None}:
                raise RuntimeError("The isolated MCP resource read returned unexpected data")
            report.update(
                sdk_version=version("mcp"),
                tool_names=sorted(tool.name for tool in first.tools),
                read_result=response.structuredContent,
                read_seconds=round(time.monotonic() - start, 3),
            )
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("idle_seconds", type=int, choices=(5, 10))
    parser.add_argument("--container")
    parser.add_argument("--native", action="store_true")
    options = parser.parse_args()
    if options.native and options.container is None:
        parser.error("--native requires a dedicated MCP container")
    print(
        json.dumps(
            asyncio.run(
                run(options.idle_seconds, container=options.container, native=options.native)
            )
        )
    )
