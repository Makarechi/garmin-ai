"""Compare Garmin MCP reads, descriptors, and revocation through pinned stdio clients."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_activity_reads import seed as seed_activities
from check_device_history import seed as seed_devices
from check_http_health_tools import seed as seed_days
from check_mcp_stdio import PYTHON_SERVER, document
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text

NAMES = (
    "health_snapshot",
    "health_range",
    "activities",
    "activity_details",
    "device_history",
    "personal_baseline",
    "analysis_compare_periods",
)
ACTIVITY_RANGE = {"start": "2026-10-25T09:00:00Z", "end": "2026-10-25T11:00:00Z"}
BASELINE = {"metric": "steps", "start": "2026-10-23", "end": "2026-10-26"}
PERIODS = {
    "metric": "steps",
    "a_start": "2026-10-23",
    "a_end": "2026-10-24",
    "b_start": "2026-10-25",
    "b_end": "2026-10-26",
}
CASES = (
    ("health_snapshot", {"day": "2026-10-24"}),
    ("health_snapshot", {"day": "2026-10-25"}),
    ("health_snapshot", {"day": "wrong"}),
    ("health_range", {"start": "2026-10-23", "end": "2026-10-26"}),
    ("health_range", {"start": "2026-10-26", "end": "2026-10-23"}),
    ("activities", ACTIVITY_RANGE),
    ("activities", {**ACTIVITY_RANGE, "kind": "running"}),
    ("activity_details", {"activity_id": "run"}),
    ("activity_details", {"activity_id": "run", "limit": 1, "include_samples": True}),
    ("activity_details", {"activity_id": "absent"}),
    ("device_history", {**ACTIVITY_RANGE, "limit": 2}),
    ("personal_baseline", BASELINE),
    ("personal_baseline", {**BASELINE, "metric": "unsupported"}),
    ("personal_baseline", {**BASELINE, "start": "not a day"}),
    ("analysis_compare_periods", PERIODS),
    ("analysis_compare_periods", {**PERIODS, "b_start": "2026-10-24"}),
    ("analysis_compare_periods", {**PERIODS, "a_start": "2026-10-25"}),
)


async def capture(command, arguments, root, env, label):
    process = StdioServerParameters(command=command, args=arguments, cwd=root, env=env)
    with (root / f"{label}.log").open("w", encoding="utf-8") as log:
        async with (
            stdio_client(process, errlog=log) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            await client.initialize()
            catalog = {tool.name: document(tool) for tool in (await client.list_tools()).tools}
            reads = []
            for name, values in CASES:
                result = document(await client.call_tool(name, values))
                if not result["isError"]:
                    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
                    assert "synthetic-private" not in json.dumps(result) or name != "device_history"
                    result["content"][0]["text"] = result["structuredContent"]
                reads.append(result)
            return {name: catalog[name] for name in NAMES}, reads


async def compare(binary, root, engine):
    env = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    for revoked in (False, True):
        if revoked:
            with engine.begin() as connection:
                connection.execute(
                    text("""
                    INSERT INTO app_state(key,value)
                    VALUES ('preferences:onboarding', jsonb_build_object('model_categories',ARRAY['diary']))
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """)
                )
        expected = await capture(sys.executable, ["-c", PYTHON_SERVER], root, env, "python")
        actual = await capture(str(binary), ["mcp"], root, env, "native")
        assert expected[0] == actual[0]
        for (name, arguments), left, right in zip(CASES, expected[1], actual[1], strict=True):
            assert left == right, (name, arguments, left["isError"], right["isError"])
        if revoked:
            assert all(case["isError"] for case in actual[1])
        else:
            assert sum(not case["isError"] for case in actual[1]) == 10


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed_days(engine)
        seed_activities(engine)
        seed_devices(engine)
        with TemporaryDirectory() as directory:
            asyncio.run(compare(args.binary.resolve(strict=True), Path(directory), engine))
    finally:
        engine.dispose()
    print(f"Verified {2 * len(CASES)} pinned MCP Garmin read and revocation cases")


if __name__ == "__main__":
    main()
