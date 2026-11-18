"""Compare Garmin MCP reads, descriptors, and revocation through pinned stdio clients."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
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

PYTHON_WITH_OWNER_SETTINGS = PYTHON_SERVER.replace(
    'engine, timezone="UTC", enable_writes=',
    "engine, timezone=settings.timezone, identity_settings=settings, enable_writes=",
)

NAMES = (
    "health_snapshot",
    "health_range",
    "activities",
    "activity_details",
    "device_history",
    "personal_baseline",
    "analysis_compare_periods",
    "data_freshness",
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
    ("personal_baseline", {**BASELINE, "metric": "sleep_score"}),
    ("personal_baseline", {**BASELINE, "metric": "unsupported"}),
    ("personal_baseline", {**BASELINE, "start": "not a day"}),
    ("analysis_compare_periods", PERIODS),
    ("analysis_compare_periods", {**PERIODS, "metric": "sleep_score"}),
    ("analysis_compare_periods", {**PERIODS, "b_start": "2026-10-24"}),
    ("analysis_compare_periods", {**PERIODS, "a_start": "2026-10-25"}),
    ("data_freshness", {}),
    ("data_freshness", {"unexpected": "synthetic"}),
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
                    if name == "data_freshness":
                        report = result["structuredContent"]
                        assert set(report) == {"checked_at", "available", "channels", "limitations"}
                        checked = datetime.fromisoformat(report.pop("checked_at"))
                        assert abs((datetime.now(UTC) - checked).total_seconds()) < 10
                        assert report["available"] == bool(report["channels"])
                        for channel in report["channels"].values():
                            window = channel.get("expected_interval")
                            if isinstance(window, dict) and isinstance(window.get("end"), str):
                                if datetime.fromisoformat(window["end"]) == checked:
                                    window["end"] = "checked_at"
                    result["content"][0]["text"] = result["structuredContent"]
                reads.append(result)
            return {name: catalog[name] for name in NAMES}, reads


async def compare(binary, root, engine):
    env = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_TIMEZONE": "UTC",
    }
    for state in ("initial", "revoked", "sleep_hidden", "replay_pending"):
        if state != "initial":
            with engine.begin() as connection:
                categories = ["diary"] if state == "revoked" else ["diary", "health"]
                connection.execute(
                    text("""
                    INSERT INTO app_state(key,value)
                    VALUES ('preferences:onboarding', jsonb_build_object('model_categories',CAST(:categories AS text[])))
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """),
                    {"categories": categories},
                )
                if state == "sleep_hidden":
                    connection.execute(text("UPDATE people SET timezone='Europe/Bratislava'"))
                    connection.execute(
                        text("""
                        INSERT INTO module_configs(id,owner_id,pack_key,tracking_enabled,collection_enabled,
                                                   reminders_enabled,visible,llm_enabled,settings,revision)
                        SELECT gen_random_uuid(),id,pack,true,true,true,true,pack<>'sleep','{}',1
                        FROM people CROSS JOIN unnest(ARRAY['general_diary','sleep','training','wellbeing']) AS pack
                        ON CONFLICT (owner_id,pack_key) DO UPDATE SET llm_enabled=EXCLUDED.llm_enabled
                        """)
                    )
                if state == "replay_pending":
                    connection.execute(
                        text("""
                        INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,archive_key,parser_version,status)
                        VALUES(gen_random_uuid(),'garmin_connect','daily_summary','synthetic-day',
                               'synthetic-pending','synthetic/archive',1,'normalized')
                        """)
                    )
        expected = await capture(
            sys.executable, ["-c", PYTHON_WITH_OWNER_SETTINGS], root, env, "python"
        )
        actual = await capture(str(binary), ["mcp"], root, env, "native")
        assert expected[0] == actual[0]
        for (name, arguments), left, right in zip(CASES, expected[1], actual[1], strict=True):
            assert left == right, (name, arguments, left["isError"], right["isError"])
        successes = {"initial": 13, "revoked": 0, "sleep_hidden": 8, "replay_pending": 1}
        assert sum(not case["isError"] for case in actual[1]) == successes[state]
        if state == "replay_pending":
            report = actual[1][-2]["structuredContent"]
            assert report["channels"] == {} and not report["available"]


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
    print(f"Verified {4 * len(CASES)} pinned MCP Garmin read and permission cases")


if __name__ == "__main__":
    main()
