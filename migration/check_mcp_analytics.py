"""Compare the native analytics descriptors, results and model-pack permission gates."""

import argparse
import asyncio
import json
import math
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_event_windows import seed_events
from check_lagged_association import seed_days
from check_mcp_stdio import PYTHON_SERVER, document
from check_migraine_windows import seed_migraine
from check_stress_hours import seed_stress
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.scenario_packs import ensure_scenario_packs

CASES = (
    (
        "metric_series",
        {
            "metric": "heart_rate_bpm",
            "start": "2026-10-25T09:00:00Z",
            "end": "2026-10-25T09:01:00Z",
            "minutes": 1,
        },
    ),
    (
        "analysis_event_windows",
        {
            "event_type": "note",
            "metric": "heart_rate_bpm",
            "start": "2026-10-25T09:00:00Z",
            "end": "2026-10-26T09:00:00Z",
        },
    ),
    (
        "analysis_lagged_association",
        {
            "metric_a": "sleep_score",
            "metric_b": "stress_avg",
            "start": "2026-10-25",
            "end": "2026-11-25",
            "lags": [-1, 0, 1],
        },
    ),
    ("analysis_stress_by_hour", {"start": "2026-09-01", "end": "2026-09-03", "timezone": "UTC"}),
    (
        "analysis_migraine_windows",
        {"metric": "sleep_score", "start": "2020-01-01", "end": "2022-09-27", "timezone": "UTC"},
    ),
)


async def capture(command, arguments, environment, root, label):
    params = StdioServerParameters(command=command, args=arguments, env=environment, cwd=root)
    with (root / f"{label}.log").open("w") as errors:
        async with (
            stdio_client(params, errlog=errors) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            await client.initialize()
            catalog = {tool.name: document(tool) for tool in (await client.list_tools()).tools}
            descriptors = [catalog[name] for name, _ in CASES]
            results = []
            for name, arguments in CASES:
                result = document(await client.call_tool(name, arguments))
                if not result["isError"]:
                    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
                    result["content"][0]["text"] = result["structuredContent"]
                results.append(result)
            return descriptors, results


def difference(expected, actual, path=()):
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.keys() != actual.keys():
            return path, sorted(expected), sorted(actual)
        for key in expected:
            found = difference(expected[key], actual[key], path + (key,))
            if found:
                return found
        return None
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return path, len(expected), len(actual)
        for index, (first, second) in enumerate(zip(expected, actual, strict=True)):
            found = difference(first, second, path + (index,))
            if found:
                return found
        return None
    if (
        path
        and (path[-1] in {"pearson_r", "mean", "median", "sd"} or "ci95" in path)
        and isinstance(expected, float)
        and isinstance(actual, float)
    ):
        if math.isclose(expected, actual, rel_tol=1e-12, abs_tol=1e-12):
            return None
    if expected != actual:
        return path, str(expected)[:300], str(actual)[:300]
    return None


async def compare(binary, engine, root):
    environment = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_TIMEZONE": "UTC",
    }
    phases = (
        "default",
        "health_only",
        "diary_only",
        "wellbeing_disabled",
        "diary_pack_disabled",
        "migraine_disabled",
    )
    for phase in phases:
        with Session(engine) as session, session.begin():
            if phase != "default":
                categories = ["health", "diary"]
                if phase == "health_only":
                    categories = ["health"]
                elif phase == "diary_only":
                    categories = ["diary"]
                session.execute(
                    text(
                        "INSERT INTO app_state(key,value) VALUES ('preferences:onboarding',CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                    ),
                    {"value": json.dumps({"model_categories": categories})},
                )
            if phase in ("wellbeing_disabled", "diary_pack_disabled", "migraine_disabled"):
                ensure_scenario_packs(session)
                session.execute(text("UPDATE module_configs SET llm_enabled=true"))
                key = {
                    "wellbeing_disabled": "wellbeing",
                    "diary_pack_disabled": "general_diary",
                    "migraine_disabled": "migraine",
                }[phase]
                session.execute(
                    text("UPDATE module_configs SET llm_enabled=false WHERE pack_key=:key"),
                    {"key": key},
                )
        expected = await capture(sys.executable, ["-c", PYTHON_SERVER], environment, root, "python")
        actual = await capture(str(binary), ["mcp"], environment, root, "native")
        mismatch = difference(expected, actual)
        assert mismatch is None, (phase, mismatch)
        results = actual[1]
        if phase == "default":
            assert all(not row["isError"] for row in results)
        elif phase == "health_only":
            assert results[1]["isError"] and results[4]["isError"]
        elif phase == "diary_only":
            assert all(row["isError"] for row in results)
        elif phase == "wellbeing_disabled":
            assert results[2]["isError"] and results[3]["isError"]
        elif phase == "migraine_disabled":
            assert results[4]["isError"]
        else:
            assert results[1]["isError"]
    print(
        "Matched five analytics descriptors, results, model categories, second-metric packs and event-pack gates"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed_events(engine)
        seed_days(engine)
        seed_stress(engine)
        seed_migraine(engine, base=date(2020, 1, 1))
        with TemporaryDirectory() as directory:
            asyncio.run(compare(args.binary.resolve(strict=True), engine, Path(directory)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
