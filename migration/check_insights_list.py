"""Compare retained insight pages, timestamp ties and model access with Python."""

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
from garmin_ai.models import Insight
from garmin_ai.scenario_packs import ensure_scenario_packs

AT = datetime(2026, 10, 25, 9, tzinfo=UTC)
BOTH = "synthetic-insights-both-reader-validation-only"
HEALTH = "synthetic-insights-health-reader-validation-only"
DIARY = "synthetic-insights-diary-reader-validation-only"
TOKENS = [
    {"key": BOTH, "scopes": ["read:health", "read:diary"]},
    {"key": HEALTH, "scopes": ["read:health"]},
    {"key": DIARY, "scopes": ["read:diary"]},
]
NAME = "insights_list"
PATH = "/tools/" + NAME


def seed_insights(engine):
    with Session(engine) as session, session.begin():
        for index in range(105):
            session.add(
                Insight(
                    id=UUID(int=10000 + index),
                    category=["sleep", "wellbeing", "migraine"][index % 3],
                    statement=f"Synthetic retained conclusion {index} — пример 😀",
                    sample_size=index,
                    effect_size=None if index % 5 == 0 else index / 3,
                    status=["candidate", "accepted", "superseded", "withdrawn"][index % 4],
                    dedup_key=f"synthetic-insight-{index}",
                    generated_at=AT + timedelta(microseconds=index // 40),
                    evidence={
                        "synthetic": True,
                        "note": "Я" * 16384,
                        "record": index,
                        "optional": None,
                        "decimal": 1.0,
                    },
                )
            )
        session.flush()
        ensure_scenario_packs(session, legacy_install=True)


def exercise(python, native):
    def compare(arguments, status=200, token=BOTH):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        replies = [
            client.post(PATH, json={"arguments": arguments}, headers=headers)
            for client in (python, native)
        ]
        assert all(reply.status_code == status for reply in replies), (
            status,
            arguments,
            [(r.status_code, r.text[:300]) for r in replies],
        )
        if status == 422:
            return None
        expected, actual = [reply.json() for reply in replies]
        assert expected == actual, (
            arguments,
            expected.get("next_cursor"),
            actual.get("next_cursor"),
        )
        return actual

    compare({}, 401, token=None)
    compare({}, 403, token=HEALTH)
    compare({}, 403, token=DIARY)
    assert len(compare({})["rows"]) == 30
    page = compare({"limit": 17})
    seen = []
    for _ in range(10):
        seen.extend(row["id"] for row in page["rows"])
        if page["next_cursor"] is None:
            break
        page = compare({"limit": 17, "cursor": page["next_cursor"]})
    else:
        raise AssertionError("Insight history did not finish")
    assert len(seen) == len(set(seen)) == 105
    assert seen == [str(UUID(int=10000 + index)) for index in reversed(range(105))]
    largest = compare({"limit": 100})
    assert len(largest["rows"]) == 100 and largest["truncated"]
    for limit in (True, "17.0", "+17", "1_7"):
        compare({"limit": limit})
    for limit in (0, 101, -1, 10**100, -(10**100), 1e100, 1.5, None, "bad", "1e2"):
        compare({"limit": limit}, 422)
    for stamp in (
        AT.isoformat(),
        "20261025T090000Z",
        "2026-W43-7T09:00:00Z",
        "2026-10-25🐕09:00:00+00:00",
        "2026-10-25T09:00:00.5+00:00:00.5",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ):
        compare({"cursor": stamp + "|" + str(UUID(int=10050))})
    stamp = (AT + timedelta(microseconds=1)).isoformat()
    identity = UUID(int=10050).hex
    reference = compare({"cursor": f"{stamp}|{identity}", "limit": 3})
    assert reference["rows"][0]["id"] == str(UUID(int=10049))
    for form in (
        f"urn:uuid:{{{identity}}}",
        identity[:7] + "-" + identity[7:],
        identity[:7] + "uuid:urn:" + identity[7:],
        "0x_" + identity[3:],
        "+" + identity[1:],
        "\u3000" + identity[1:],
        "".join(chr(ord(digit) + 0xFEE0) for digit in identity),
    ):
        assert compare({"cursor": f"{stamp}|{form}", "limit": 3}) == reference
    for cursor in (
        "",
        "bad",
        AT.isoformat() + "|bad",
        "2026-10-25T09:00:00|" + str(UUID(int=10000)),
        "9999-12-31T24:00:00+01:00|" + str(UUID(int=10000)),
        f"{stamp}|{identity[:3]}__{identity[5:]}",
        f"{stamp}|{identity[:3]}\u00b2{identity[4:]}",
        f"{stamp}|\x1c{identity[1:]}",
        f"{stamp}|URN:UUID:{UUID(int=10050)}",
        "x" * 201,
    ):
        compare({"cursor": cursor}, 422)
    print(
        "Matched all 105 tied insight records, retained statuses/evidence, 100-row pages and calendar-crossing cursors"
    )


async def capture(command, args, environment, root, label):
    params = StdioServerParameters(command=command, args=args, env=environment, cwd=root)
    with (root / f"{label}.log").open("w") as errors:
        async with (
            stdio_client(params, errlog=errors) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            await client.initialize()
            descriptor = next(
                document(tool) for tool in (await client.list_tools()).tools if tool.name == NAME
            )
            stamp = (AT + timedelta(microseconds=1)).isoformat()
            identity = UUID(int=10050)
            responses = []
            for arguments in [
                {"limit": 100},
                {"limit": -1},
                {"limit": 10**100},
                {"limit": 1e100},
                {"limit": 3, "cursor": f"{stamp}|urn:uuid:{{{identity}}}"},
                {"limit": 3, "cursor": f"{stamp}|URN:UUID:{identity}"},
            ]:
                response = document(await client.call_tool(NAME, arguments))
                if not response["isError"]:
                    assert (
                        json.loads(response["content"][0]["text"]) == response["structuredContent"]
                    )
                    response["content"][0]["text"] = response["structuredContent"]
                responses.append(response)
            return descriptor, responses


async def mcp_compare(binary, engine, root):
    environment = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    for phase in (
        "default",
        "health_only",
        "diary_only",
        "sleep_disabled",
        "wellbeing_disabled",
        "migraine_disabled",
    ):
        with Session(engine) as session, session.begin():
            categories = ["health", "diary"]
            if phase == "health_only":
                categories = ["health"]
            if phase == "diary_only":
                categories = ["diary"]
            session.execute(
                text(
                    "INSERT INTO app_state(key,value) VALUES ('preferences:onboarding',CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                ),
                {"value": json.dumps({"model_categories": categories})},
            )
            session.execute(text("UPDATE module_configs SET llm_enabled=true"))
            if phase.endswith("_disabled"):
                session.execute(
                    text("UPDATE module_configs SET llm_enabled=false WHERE pack_key=:key"),
                    {"key": phase.removesuffix("_disabled")},
                )
        expected = await capture(sys.executable, ["-c", PYTHON_SERVER], environment, root, "python")
        actual = await capture(str(binary), ["mcp"], environment, root, "native")
        assert expected == actual, (
            phase,
            [r["isError"] for r in expected[1]],
            [r["isError"] for r in actual[1]],
        )
        assert actual[1][0]["isError"] == (phase != "default")
        if phase == "default":
            assert actual[1][4]["structuredContent"]["rows"][0]["id"] == str(UUID(int=10049))
            assert actual[1][5]["isError"]
    print(
        "Matched pinned MCP descriptor, streamed evidence and health/diary/three-pack restrictions"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    engine = isolated_engine()
    try:
        seed_insights(engine)
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
                with httpx.Client(base_url=url, timeout=120, trust_env=False) as native:
                    exercise(python, native)
            finally:
                server.terminate()
                server.wait(timeout=5)
        with TemporaryDirectory() as directory:
            asyncio.run(mcp_compare(binary, engine, Path(directory)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
