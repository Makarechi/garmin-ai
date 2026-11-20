"""Compare caffeine/sleep cohorts, exclusions and byte-exact evidence hashes."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from capture_reference import isolated_engine
from check_mcp_stdio import PYTHON_SERVER, document
from check_migraine_windows import differences
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import ensure_system_definition
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import Event, HealthDay, TimelineInterval
from garmin_ai.scenario_packs import ensure_scenario_packs

BASE = date(2026, 10, 1)
BOTH = "synthetic-coffee-both-reader-validation-only"
HEALTH = "synthetic-coffee-health-reader-validation-only"
DIARY = "synthetic-coffee-diary-reader-validation-only"
TOKENS = [
    {"key": BOTH, "scopes": ["read:health", "read:diary"]},
    {"key": HEALTH, "scopes": ["read:health"]},
    {"key": DIARY, "scopes": ["read:diary"]},
]
NAME = "analysis_coffee_sleep"
PATH = "/tools/" + NAME
REQUEST = {"start": str(BASE), "end": str(BASE + timedelta(days=30))}


def seed_coffee(engine, base=BASE):
    with Session(engine) as session, session.begin():
        for index in range(31):
            day = base + timedelta(days=index)
            finish = datetime.combine(day, datetime.min.time(), UTC) + timedelta(
                hours=7, microseconds=index * 123
            )
            start = finish - timedelta(hours=8)
            source = f"synthetic:кофе-😀:{index}"
            session.add(
                HealthDay(
                    day=day,
                    sleep_score=60 + index / 3,
                    sleep_seconds=28000 + index * 123.25,
                    sources={"field:sleep_score": source, "field:sleep_seconds": source},
                )
            )
            session.add(
                TimelineInterval(
                    id=f"sleep:{day}",
                    start=start,
                    end=finish,
                    label="sleep",
                    source="garmin",
                    confidence=1,
                    evidence={"source_ref": source},
                )
            )
            session.flush()
            create_event(
                session,
                EventInput(
                    start=start - timedelta(hours=24),
                    end=start,
                    payload={
                        "type": "caffeine_log_complete",
                        "description": "Synthetic explicit coverage",
                    },
                ),
                actor="synthetic-owner",
            )
            create_event(
                session,
                EventInput(
                    start=start - timedelta(hours=4 if index % 2 else 8),
                    payload={
                        "type": "caffeine",
                        "beverage": "Synthetic drink",
                        "dose_basis": "per_serving",
                        "servings": 1.5,
                        "caffeine_mg_min": 33.3,
                        "caffeine_mg_estimate": 50.1,
                        "caffeine_mg_max": 90,
                    },
                ),
                actor="synthetic-owner",
            )
        # Older stored numeric fields retain integer spelling in the evidence hash.
        session.execute(
            text(
                "UPDATE events SET payload=payload || CAST(:payload AS jsonb) WHERE kind='caffeine' AND start=(SELECT min(start) FROM events WHERE kind='caffeine')"
            ),
            {
                "payload": json.dumps(
                    {
                        "dose_basis": "total",
                        "caffeine_mg_min": 50,
                        "caffeine_mg_estimate": 60,
                        "caffeine_mg_max": 70,
                    }
                )
            },
        )


def exercise(engine, python, native):
    def compare(arguments, status=200, token=BOTH):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        replies = [
            client.post(PATH, json={"arguments": arguments}, headers=headers)
            for client in (python, native)
        ]
        assert all(reply.status_code == status for reply in replies), (
            arguments,
            status,
            [(r.status_code, r.text[:1200]) for r in replies],
        )
        if status == 422:
            return None
        expected, actual = [reply.json() for reply in replies]
        mismatch = differences(expected, actual)
        assert mismatch is None, (arguments, mismatch)
        return actual

    compare(REQUEST, 401, token=None)
    compare(REQUEST, 403, token=HEALTH)
    compare(REQUEST, 403, token=DIARY)
    result = compare(REQUEST)
    assert result["eligible"] == 31 and result["status"] == "exploratory"
    assert result["comparison"]["ci95"] is not None
    explicit = compare(REQUEST | {"late_hours": 6})
    assert result["evidence_hash"] != explicit["evidence_hash"]
    compare(REQUEST | {"outcome": "sleep_seconds"})
    for hours in (1, True, "6", "1_2", "+_1", "1._2", "\u20036\u2003", 6.000000000138889, 24):
        compare(REQUEST | {"late_hours": hours})
    compare(REQUEST | {"end": str(BASE)})
    small = compare(REQUEST | {"end": str(BASE + timedelta(days=9))})
    assert small["status"] == "exploratory" and small["comparison"]["ci95"] is None
    compare({"start": "2020-01-01", "end": "2020-01-02"})
    for invalid in (
        {"late_hours": 0},
        {"late_hours": 24.1},
        {"late_hours": None},
        {"late_hours": "nan"},
        {"late_hours": "1__2"},
        {"late_hours": "_1"},
        {"late_hours": "1_"},
        {"late_hours": "١٢"},
        {"outcome": "stress_avg"},
        {"outcome": None},
        {"end": str(BASE + timedelta(days=31))},
        {"start": REQUEST["end"], "end": REQUEST["start"]},
    ):
        compare(REQUEST | invalid, 422)
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE events SET status='needs_confirmation' WHERE kind='caffeine'"))
    pending = compare(REQUEST)
    assert pending["eligible"] == 0 and all(
        row["total_caffeine_mg"]["min"] is None for row in pending["rows"]
    )
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE events SET status='confirmed' WHERE kind='caffeine'"))
        session.execute(text("UPDATE events SET deleted=true WHERE kind='caffeine'"))
    absent = compare(REQUEST)
    assert absent["eligible"] == 31 and all(
        row["total_caffeine_mg"]["min"] == 0 for row in absent["rows"]
    )
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE events SET deleted=false WHERE kind='caffeine'"))
        session.execute(text("UPDATE events SET deleted=true WHERE kind='caffeine_log_complete'"))
    incomplete = compare(REQUEST)
    assert incomplete["eligible"] == 0 and all(
        not row["diary_complete"] for row in incomplete["rows"]
    )
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE events SET deleted=false WHERE kind='caffeine_log_complete'"))
        version = ensure_system_definition(session, "illness")
        session.add(
            Event(
                id=UUID(int=9900),
                definition_version_id=version.id,
                topology=version.topology,
                kind="illness",
                start=datetime(2020, 1, 1, tzinfo=UTC),
                timezone="UTC",
                source="manual",
                status="confirmed",
                payload={"type": "illness", "description": "Synthetic old open episode"},
            )
        )
    compare(REQUEST, 422)
    ill = compare(REQUEST | {"end": str(BASE + timedelta(days=6))})
    assert ill["eligible"] == 0 and all(
        "recorded_illness_or_travel" in row["exclusions"] for row in ill["rows"]
    )
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE events SET deleted=true WHERE id=:id"), {"id": UUID(int=9900)})
    assert compare(REQUEST)["evidence_hash"] == result["evidence_hash"]
    one_day = REQUEST | {"end": str(BASE)}
    bedtime = datetime.combine(BASE, datetime.min.time(), UTC) - timedelta(hours=1)
    with Session(engine) as session, session.begin():
        version = ensure_system_definition(session, "caffeine_absence")
        session.add(
            Event(
                id=UUID(int=9910),
                definition_version_id=version.id,
                topology=version.topology,
                kind="caffeine_absence",
                timezone="UTC",
                source="manual",
                status="confirmed",
                start=bedtime - timedelta(hours=24),
                end=bedtime,
                payload={
                    "type": "caffeine_absence",
                    "description": "Synthetic contradictory coverage",
                },
            )
        )
    conflict = compare(one_day)
    assert conflict["rows"][0]["coverage_conflict"] and not conflict["rows"][0]["eligible"]
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE events SET deleted=true WHERE id=:id"), {"id": UUID(int=9910)})
        session.execute(
            text(
                "UPDATE health_days SET sources=jsonb_set(sources,'{field:sleep_score}',to_jsonb('synthetic:mismatched'::text)) WHERE day=:day"
            ),
            {"day": BASE},
        )
    mismatch = compare(one_day)
    assert "inconsistent_sleep_source" in mismatch["rows"][0]["exclusions"]
    with Session(engine) as session, session.begin():
        session.execute(
            text(
                "UPDATE health_days SET sources=jsonb_set(sources,'{field:sleep_score}',to_jsonb(CAST(:source AS text))),sleep_score=NULL WHERE day=:day"
            ),
            {"source": "synthetic:кофе-😀:0", "day": BASE},
        )
    missing = compare(one_day)
    assert "missing_outcome" in missing["rows"][0]["exclusions"]
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE health_days SET sleep_score=60 WHERE day=:day"), {"day": BASE})
        session.execute(
            text(
                "UPDATE events SET payload=jsonb_set(payload,'{dose_basis}',to_jsonb('unknown'::text)) WHERE kind='caffeine' AND start=:at"
            ),
            {"at": bedtime - timedelta(hours=8)},
        )
    unknown = compare(one_day)
    assert unknown["rows"][0]["eligible"] and unknown["rows"][0]["total_caffeine_mg"]["min"] is None
    with Session(engine) as session, session.begin():
        session.execute(
            text(
                "UPDATE events SET payload=jsonb_set(payload,'{dose_basis}',to_jsonb('total'::text)) WHERE kind='caffeine' AND start=:at"
            ),
            {"at": bedtime - timedelta(hours=8)},
        )
    assert compare(REQUEST)["evidence_hash"] == result["evidence_hash"]
    with Session(engine) as session, session.begin():
        version = ensure_system_definition(session, "caffeine")
        for index in range(998):
            session.add(
                Event(
                    id=UUID(int=700000 + index),
                    definition_version_id=version.id,
                    topology=version.topology,
                    kind="caffeine",
                    timezone="UTC",
                    source="manual",
                    status="confirmed",
                    start=bedtime - timedelta(hours=1),
                    deleted=index == 997,
                    payload={
                        "type": "caffeine",
                        "beverage": "Synthetic budget input",
                        "dose_basis": "unknown",
                    },
                )
            )
    compare(one_day, 422)
    with Session(engine) as session, session.begin():
        session.execute(
            text("UPDATE events SET deleted=false WHERE id=:id"), {"id": UUID(int=700997)}
        )
    compare(one_day, 422)
    with Session(engine) as session, session.begin():
        session.execute(
            text("UPDATE events SET deleted=true WHERE id BETWEEN :first AND :last"),
            {"first": UUID(int=700000), "last": UUID(int=700997)},
        )
    assert compare(REQUEST)["evidence_hash"] == result["evidence_hash"]
    print(
        "Matched cohorts, seeded intervals, dose types, explicit coverage, exclusions and exact evidence hashes"
    )


async def mcp_capture(command, args, environment, root, label):
    params = StdioServerParameters(command=command, args=args, env=environment, cwd=root)
    with (root / f"{label}.log").open("w") as errors:
        async with (
            stdio_client(params, errlog=errors) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            await client.initialize()
            catalog = {tool.name: document(tool) for tool in (await client.list_tools()).tools}
            result = document(await client.call_tool(NAME, REQUEST))
            if not result["isError"]:
                assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
                result["content"][0]["text"] = result["structuredContent"]
            return [catalog[NAME], result]


async def mcp_compare(binary, engine, root):
    environment = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    phases = [
        ("default", ["health", "diary"]),
        ("health_only", ["health"]),
        ("diary_only", ["diary"]),
        ("sleep_disabled", ["health", "diary"]),
        ("caffeine_disabled", ["health", "diary"]),
    ]
    for phase, categories in phases:
        with Session(engine) as session, session.begin():
            session.execute(
                text(
                    "INSERT INTO app_state(key,value) VALUES ('preferences:onboarding',CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                ),
                {"value": json.dumps({"model_categories": categories})},
            )
            if phase.endswith("_disabled"):
                ensure_scenario_packs(session)
                session.execute(text("UPDATE module_configs SET llm_enabled=true"))
                session.execute(
                    text("UPDATE module_configs SET llm_enabled=false WHERE pack_key=:key"),
                    {"key": phase.removesuffix("_disabled")},
                )
        expected = await mcp_capture(
            sys.executable, ["-c", PYTHON_SERVER], environment, root, "python"
        )
        actual = await mcp_capture(str(binary), ["mcp"], environment, root, "native")
        mismatch = differences(expected, actual)
        assert mismatch is None, (phase, mismatch)
        assert actual[1]["isError"] == (phase != "default")
    print("Matched pinned MCP descriptor and health/diary/caffeine/sleep model access")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    engine = isolated_engine()
    try:
        seed_coffee(engine)
        settings = Settings(_env_file=None, api_tokens=[ApiToken(**token) for token in TOKENS])
        with (
            TestClient(create_app(settings, engine)) as python,
            subprocess.Popen(
                [binary, "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                    "GA_API_TOKENS": json.dumps(TOKENS),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as server,
        ):
            try:
                url = server.stdout.readline().strip().removeprefix("Listening on ")
                assert url.startswith("http://127.0.0.1:")
                with httpx.Client(base_url=url, timeout=15, trust_env=False) as native:
                    exercise(engine, python, native)
            finally:
                server.terminate()
                server.wait(timeout=5)
        with TemporaryDirectory() as directory:
            asyncio.run(mcp_compare(binary, engine, Path(directory)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
