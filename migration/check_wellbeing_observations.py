"""Compare bounded subjective report pages and whole-range summaries with Python."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from capture_reference import isolated_engine
from check_mcp_stdio import PYTHON_SERVER, document
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import ensure_system_definition
from garmin_ai.models import Event
from garmin_ai.scenario_packs import ensure_scenario_packs

AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
DIARY = "synthetic-wellbeing-diary-reader-validation-only"
HEALTH = "synthetic-wellbeing-health-reader-validation-only"
TOKENS = [{"key": DIARY, "scopes": ["read:diary"]}, {"key": HEALTH, "scopes": ["read:health"]}]
NAME = "wellbeing_observations"
PATH = "/tools/" + NAME
REQUEST = {"start": AT.isoformat(), "end": (AT + timedelta(days=1)).isoformat()}


def seed_reports(engine):
    with Session(engine) as session, session.begin():
        version = ensure_system_definition(session, "wellbeing_observation")
        notes = [None, "", "Synthetic note", "я" * 2000, "я" * 2001, "😀" * 4000]
        for index in range(260):
            payload = {
                "type": "wellbeing_observation",
                "energy": index % 11,
                "restedness": None if index % 3 else index % 11,
                "pain": None if index % 5 else index % 11,
                "functional_impact": None if index % 7 else index % 11,
                "notes": notes[index % len(notes)],
            }
            session.add(
                Event(
                    id=UUID(int=10000 + index),
                    definition_version_id=version.id,
                    topology=version.topology,
                    kind="wellbeing_observation",
                    start=AT + timedelta(microseconds=index // 80),
                    timezone="UTC",
                    source="user" if index == 259 else "manual",
                    status="confirmed",
                    payload=payload,
                    original_text="Synthetic private source " + "я" * 16000
                    if index < 2
                    else "Synthetic private source",
                    idempotency_key=f"synthetic-wellbeing-{index}",
                )
            )
        for index, (offset, source, status, deleted) in enumerate(
            [
                (timedelta(), "inferred", "inferred", False),
                (timedelta(), "wearable", "confirmed", False),
                (timedelta(), "manual", "needs_confirmation", False),
                (timedelta(), "manual", "confirmed", True),
                (timedelta(days=1), "manual", "confirmed", False),
                (timedelta(microseconds=-1), "manual", "confirmed", False),
            ]
        ):
            session.add(
                Event(
                    id=UUID(int=20000 + index),
                    kind="wellbeing_observation",
                    start=AT + offset,
                    timezone="UTC",
                    source=source,
                    status=status,
                    deleted=deleted,
                    payload={"type": "wellbeing_observation", "energy": 10},
                )
            )


def exercise(python, native):
    def compare(arguments, status=200, token=DIARY):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        replies = [
            client.post(PATH, json={"arguments": arguments}, headers=headers)
            for client in (python, native)
        ]
        assert all(reply.status_code == status for reply in replies), (
            arguments,
            status,
            [(r.status_code, r.text[:600]) for r in replies],
        )
        if status != 422:
            expected, actual = [reply.json() for reply in replies]
            assert expected == actual, (arguments, expected, actual)
            return actual
        return None

    compare(REQUEST, 401, token=None)
    compare(REQUEST, 403, token=HEALTH)
    page = compare(REQUEST)
    assert page["summary"]["reports"] == 260
    summary = page["summary"]
    seen = []
    for _ in range(260):
        assert page["summary"] == summary and page["rows"]
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 20000
        for row in page["rows"]:
            assert "original_text" not in row and "idempotency_key" not in row
            assert row["omitted_fields"] == ["original_text", "idempotency_key"]
            assert row["payload"].get("notes") is None or len(row["payload"]["notes"]) <= 2000
            seen.append(row["id"])
        if page["next_cursor"] is None:
            break
        page = compare(REQUEST | {"cursor": page["next_cursor"]})
    else:
        raise AssertionError("Report pagination did not finish")
    assert len(seen) == len(set(seen)) == 260
    compare(REQUEST | {"start": (AT + timedelta(hours=1)).isoformat()})
    compare(REQUEST | {"end": (AT + timedelta(days=31)).isoformat()})
    for stamp in [
        AT.isoformat(),
        "20261025T090000Z",
        "2026-W43-7T09:00:00Z",
        "2026W437T090000Z",
        "2026-10-25🐕09:00:00+00:00",
        "2026-10-25T09:00:30+00:00:30",
        "2026-10-25T09:00:00.1234567+00:00",
        "2026-10-25T09:00:00.5+00:00:00.5",
        "2026-10-25T24:00:00Z",
        "2026-10-25T090000xZ",
        "2026-10-25T09:00:00Z\0",
    ]:
        compare(REQUEST | {"cursor": json.dumps([stamp, str(UUID(int=10000))])})
    compare(REQUEST | {"cursor": json.dumps({AT.isoformat(): None, str(UUID(int=10000)): None})})
    compare(REQUEST | {"start": "2026-10-25T11:00:00+02:00"})
    for cursor in [
        "",
        "null",
        "[]",
        "{}",
        "[1,2]",
        json.dumps(["2026-298T09:00:00Z", str(UUID(int=10000))]),
        json.dumps([AT.isoformat(), "bad"]),
        json.dumps([AT.replace(tzinfo=None).isoformat(), str(UUID(int=10000))]),
        json.dumps([(AT - timedelta(seconds=1)).isoformat(), str(UUID(int=10000))]),
        json.dumps([(AT + timedelta(days=1)).isoformat(), str(UUID(int=10000))]),
    ]:
        compare(REQUEST | {"cursor": cursor}, 422)
    compare(REQUEST | {"end": REQUEST["start"]}, 422)
    compare(REQUEST | {"end": (AT + timedelta(days=31, microseconds=1)).isoformat()}, 422)
    compare(REQUEST | {"cursor": True}, 422)
    print(
        "Matched all 260 reports across byte-bounded shared-timestamp pages, full summaries, omissions, filters and cursors"
    )


async def mcp_capture(command, arguments, environment, root, label):
    params = StdioServerParameters(command=command, args=arguments, env=environment, cwd=root)
    with (root / f"{label}.log").open("w") as errors:
        async with (
            stdio_client(params, errlog=errors) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            await client.initialize()
            catalog = {tool.name: document(tool) for tool in (await client.list_tools()).tools}
            response = document(await client.call_tool(NAME, REQUEST))
            if not response["isError"]:
                assert json.loads(response["content"][0]["text"]) == response["structuredContent"]
                response["content"][0]["text"] = response["structuredContent"]
            return catalog[NAME], response


async def check_mcp(binary, engine, root):
    environment = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    for phase, categories in [
        ("diary_only", ["diary"]),
        ("health_only", ["health"]),
        ("pack_disabled", ["diary"]),
    ]:
        with Session(engine) as session, session.begin():
            session.execute(
                text(
                    "INSERT INTO app_state(key,value) VALUES ('preferences:onboarding',CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                ),
                {"value": json.dumps({"model_categories": categories})},
            )
            if phase == "pack_disabled":
                ensure_scenario_packs(session)
                session.execute(
                    text("UPDATE module_configs SET llm_enabled=false WHERE pack_key='wellbeing'")
                )
        expected = await mcp_capture(
            sys.executable, ["-c", PYTHON_SERVER], environment, root, "python"
        )
        actual = await mcp_capture(str(binary), ["mcp"], environment, root, "native")
        assert expected == actual, (phase, expected, actual)
        assert actual[1]["isError"] == (phase != "diary_only")
    print("Matched pinned MCP descriptor, diary-only access and category/pack refusals")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    engine = isolated_engine()
    try:
        seed_reports(engine)
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
                    exercise(python, native)
            finally:
                server.terminate()
                server.wait(timeout=5)
        with TemporaryDirectory() as directory:
            asyncio.run(check_mcp(binary, engine, Path(directory)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
