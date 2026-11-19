"""Compare native MCP sleep reads with the frozen Python MCP application entry point."""

import argparse
import asyncio
import json
import math
import sys
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_mcp_stdio import document
from check_sleep_analysis import seed as seed_analysis
from check_sleep_timing import CASES, DAY
from check_sleep_timing import seed as seed_timing
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.scenario_packs import ensure_scenario_packs

NAME = "analysis_sleep"


async def capture(command, args, environment, root, label, cases):
    params = StdioServerParameters(command=command, args=args, env=environment, cwd=root)
    with (root / f"{label}.log").open("w") as errors:
        async with (
            stdio_client(params, errlog=errors) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            try:
                await client.initialize()
            except Exception:
                errors.flush()
                # Diagnostic output is limited to the synthetic fixture and excludes its connection URL.
                diagnostic = (root / f"{label}.log").read_text()
                diagnostic = diagnostic.replace(environment["GA_DATABASE_URL"], "<redacted>")
                password = make_url(environment["GA_DATABASE_URL"]).password
                if password:
                    diagnostic = diagnostic.replace(password, "<redacted>")
                raise RuntimeError(diagnostic[-2000:]) from None
            descriptor = next(
                document(tool) for tool in (await client.list_tools()).tools if tool.name == NAME
            )
            responses = []
            for arguments in cases:
                response = document(await client.call_tool(NAME, arguments))
                if not response["isError"]:
                    assert (
                        json.loads(response["content"][0]["text"]) == response["structuredContent"]
                    )
                    response["content"][0]["text"] = response["structuredContent"]
                responses.append(response)
            return descriptor, responses


async def compare(binary, root, engine, cases, phase):
    environment = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_TIMEZONE": "UTC",
    }
    expected = await capture(
        sys.executable, ["-m", "garmin_ai.mcp_server"], environment, root, "python", cases
    )
    actual = await capture(str(binary), ["mcp"], environment, root, "native", cases)

    def difference(left, right, path=""):
        if left == right:
            return None
        if path.endswith((".bedtime_circular_sd_minutes", ".wake_time_circular_sd_minutes")):
            if (
                isinstance(left, (int, float))
                and isinstance(right, (int, float))
                and math.isclose(left, right, rel_tol=0, abs_tol=1e-12)
            ):
                return None
        if isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys():
            for key in left:
                found = difference(left[key], right[key], path + "." + key)
                if found:
                    return found
            return None
        if (
            isinstance(left, (list, tuple))
            and isinstance(right, (list, tuple))
            and len(left) == len(right)
        ):
            for index, (first, second) in enumerate(zip(left, right, strict=True)):
                found = difference(first, second, path + f"[{index}]")
                if found:
                    return found
            return None
        return path, str(left)[:300], str(right)[:300]

    found = difference(expected, actual)
    assert found is None, (phase, found)
    return actual[1]


async def verify(binary, root, engine):
    cases = [
        {"start": "2026-10-24", "end": "2026-10-29"},
        {"start": "2026-10-24", "end": "2026-10-29", "nap_policy": "include_confirmed"},
        {"start": "2026-10-11", "end": "2026-11-10"},
        {"start": "2026-10-30", "end": "2026-10-30"},
        {"start": "2026-10-25", "end": "2026-10-25", "timing_only": True},
        {"start": "2026-10-25", "end": "2026-10-26", "timing_only": True},
        {
            "start": "2026-10-25",
            "end": "2026-10-25",
            "timing_only": True,
            "nap_policy": "include_confirmed",
        },
        {"start": "2026-10-01", "end": "2026-11-01"},
        {"start": "2026-10-25", "end": "2026-10-24"},
        {"start": "2026-10-25", "end": "2026-10-25", "timing_only": "true"},
        {"start": "2026-10-25", "end": "2026-10-25", "timing_only": None},
        {"start": "2026-10-25", "end": "2026-10-25", "unknown": True},
    ]
    for phase in (
        "Europe/Bratislava",
        "Pacific/Kiritimati",
        "UTC",
        "health_only",
        "diary_only",
        "sleep_off",
    ):
        with Session(engine) as session, session.begin():
            categories = (
                ["health"]
                if phase == "health_only"
                else ["diary"]
                if phase == "diary_only"
                else ["health", "diary"]
            )
            session.execute(
                text(
                    "INSERT INTO app_state(key,value) VALUES ('preferences:onboarding',CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                ),
                {"value": json.dumps({"model_categories": categories})},
            )
            session.execute(
                text("UPDATE people SET timezone=:zone WHERE singleton"),
                {"zone": phase if "/" in phase or phase == "UTC" else "Europe/Bratislava"},
            )
            session.execute(text("UPDATE module_configs SET llm_enabled=true"))
            if phase == "sleep_off":
                session.execute(
                    text("UPDATE module_configs SET llm_enabled=false WHERE pack_key='sleep'")
                )
        result = await compare(binary, root, engine, cases, phase)
        assert result[0]["isError"] == (phase in {"health_only", "diary_only", "sleep_off"})
    print(
        "Matched actual Python MCP sleep descriptor, 31-day summaries, nap policies, three owner timezones, both model categories and disabled sleep pack"
    )

    with Session(engine) as session, session.begin():
        session.execute(text("DELETE FROM timeline_intervals"))
        session.execute(
            text("DELETE FROM app_state WHERE key LIKE 'ingest:%' OR key LIKE 'ingest-meta:%'")
        )
        session.execute(text("DELETE FROM source_payloads"))
        session.execute(text("UPDATE people SET timezone='Europe/Bratislava' WHERE singleton"))
        session.execute(
            text(
                "UPDATE app_state SET value=CAST(:value AS jsonb) WHERE key='preferences:onboarding'"
            ),
            {"value": json.dumps({"model_categories": ["health", "diary"]})},
        )
        session.execute(text("UPDATE module_configs SET llm_enabled=true"))
        seed_timing(session)
    timing = [
        {
            "start": str(DAY + timedelta(days=index)),
            "end": str(DAY + timedelta(days=index)),
            "timing_only": True,
        }
        for index in range(len(CASES))
    ]
    timing += [
        {"start": str(DAY), "end": str(DAY)},
        {"start": str(DAY), "end": str(DAY), "timing_only": "true"},
        {"start": str(DAY), "end": str(DAY), "timing_only": 1},
        {"start": str(DAY), "end": str(DAY), "timing_only": True, "unknown": True},
        {"start": str(DAY), "end": str(DAY + timedelta(days=1)), "timing_only": True},
    ]
    result = await compare(binary, root, engine, timing, "pending_replay")
    assert result[0]["structuredContent"]["status"] == "available"
    assert all(row["isError"] for row in result[len(CASES) :])
    print(
        f"Matched {len(CASES)} exact timing decisions during pending history replay, including verified source and failed/unconfirmed/mismatched provenance"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    binary = parser.parse_args().binary.resolve(strict=True)
    engine = isolated_engine()
    try:
        with Session(engine) as session:
            seed_analysis(session)
            session.execute(
                text(
                    "UPDATE events SET payload=payload || '{\"description\":\"Synthetic reported nap\"}'::jsonb WHERE kind='nap'"
                )
            )
            session.commit()
        with Session(engine) as session, session.begin():
            ensure_scenario_packs(session, legacy_install=True)
        with TemporaryDirectory() as directory:
            asyncio.run(verify(binary, Path(directory), engine))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
