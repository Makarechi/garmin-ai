"""Compare nonfinite generic thresholds at the raw stdio boundary."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from capture_reference import isolated_engine
from check_generic_interfaces import (
    configure_phase,
    seed_observations,
    seed_pairs,
    seed_thresholds,
)
from check_mcp_stdio import PYTHON_SERVER
from generic_threshold_fixtures import request as threshold_request

INVALID_JSON = (b"NaN", b"Infinity", b"-Infinity")
OUT_OF_RANGE = (b"1e400", b"-1e400")
FINITE = b"20.0"


async def exchange(command, environment):
    process = await asyncio.create_subprocess_exec(
        *command,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def send(document, number=None):
        payload = json.dumps(document).encode()
        if number is not None:
            payload = payload.replace(b'"synthetic_threshold_value"', number)
        process.stdin.write(payload + b"\n")
        await process.stdin.drain()

    async def receive():
        line = await process.stdout.readline()
        assert line, "MCP closed before its next response"
        return json.loads(line)

    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-threshold", "version": "1"},
                },
            }
        )
        assert (await asyncio.wait_for(receive(), 10))["id"] == 1
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        spec = threshold_request(source="measurement:basic", threshold="synthetic_threshold_value")
        outcomes = {}
        for identity, number in enumerate((*INVALID_JSON, *OUT_OF_RANGE, FINITE), start=2):
            identity *= 2
            await send(
                {
                    "jsonrpc": "2.0",
                    "id": identity,
                    "method": "tools/call",
                    "params": {"name": "generic_analysis", "arguments": {"spec": spec}},
                },
                number,
            )
            try:
                response = await asyncio.wait_for(receive(), 2 if number in INVALID_JSON else 10)
            except TimeoutError:
                assert number in INVALID_JSON
                response = None
            if response is not None:
                assert response["id"] == identity
                result = response["result"]
                if number == FINITE:
                    assert result["isError"] is False
                    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
                    result["content"][0]["text"] = result["structuredContent"]
                else:
                    assert result["isError"] is True
            outcomes[number] = response
            await send({"jsonrpc": "2.0", "id": identity + 1, "method": "ping"})
            ping = await asyncio.wait_for(receive(), 10)
            assert ping["id"] == identity + 1 and ping["result"] == {}
        process.stdin.close()
        assert await asyncio.wait_for(process.wait(), 5) == 0
        return outcomes
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
        owner = seed_observations(engine)
        seed_pairs(engine, owner)
        seed_thresholds(engine, owner)
        configure_phase(engine, "facts")
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        }
        python = asyncio.run(exchange([sys.executable, "-c", PYTHON_SERVER], environment))
        rust = asyncio.run(exchange([args.binary.resolve(strict=True), "mcp"], environment))
        for number in (*OUT_OF_RANGE, FINITE):
            assert python[number] == rust[number], number
        for number in INVALID_JSON:
            assert python[number] is not None and rust[number] is None, number
        print(
            "Matched both numeric overflows, a finite threshold result and recovery after invalid frames"
        )
        print("Malformed NaN and Infinity frames retain the documented stdio response difference")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
