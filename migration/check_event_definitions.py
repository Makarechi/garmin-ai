"""Compare paged immutable definition discovery and model schema sharing with Python."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from capture_reference import isolated_engine
from check_mcp_stdio import PYTHON_SERVER, document
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import ensure_system_definitions
from garmin_ai.models import (
    AppState,
    EventDefinition,
    EventDefinitionVersion,
    Person,
    SourcePayload,
)
from garmin_ai.replay import replay_pending_condition
from garmin_ai.share_policy import TrackerShareConsent

DIARY = "synthetic-definition-diary-reader-validation-only"
HEALTH = "synthetic-definition-health-reader-validation-only"
TOKENS = [
    {"key": DIARY, "scopes": ["read:diary"]},
    {"key": HEALTH, "scopes": ["read:health"]},
]
NAME = "event_definitions"
PATH = "/tools/" + NAME


def seed(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id))
        assert owner is not None
        ensure_system_definitions(session)
        session.add(
            SourcePayload(
                source="garmin_connect",
                endpoint="daily",
                source_key="2026-10-25",
                payload_hash="b" * 64,
                archive_key="sha256:" + "b" * 64,
                fetched_at=datetime(2026, 10, 25, tzinfo=UTC),
                parser_version=0,
                status="pending",
                payload={"totalSteps": 0},
            )
        )
        session.flush()
        assert session.scalar(select(replay_pending_condition()))
        for index in range(90):
            identity = UUID(int=20000 + index)
            key = f"user.a{index:03d}" if index < 70 else f"user.z{index - 70:03d}"
            current = None if index == 71 else 8 if index == 70 else 1
            session.add(
                EventDefinition(
                    id=identity,
                    owner_id=owner,
                    namespace="user",
                    key=key,
                    status="draft"
                    if current is None
                    else "retired"
                    if index % 3 == 0
                    else "active",
                    current_version=current,
                    revision=index + 1,
                )
            )
            session.flush()
            for version in range(1, (current or 0) + 1):
                session.add(
                    EventDefinitionVersion(
                        id=UUID(int=30000 + index * 10 + version),
                        definition_id=identity,
                        version=version,
                        schema={"type": "object", "description": "Я" * 16384, "version": version},
                        schema_hash=f"{version:064x}",
                        topology="point",
                        field_metadata={"note": {"id": key + ".note", "semantic": "text"}},
                        labels={"ru": "Пример 😀", "en": "Synthetic tracker"},
                        privacy="sensitive"
                        if index < 70 or (index == 70 and version % 2)
                        else "private",
                        allowed_operations=["query", "update"],
                    )
                )
        session.flush()


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
            [(r.status_code, r.text[:500]) for r in replies],
        )
        if status != 200:
            return None
        expected, actual = [reply.json() for reply in replies]
        assert expected == actual, arguments
        return actual

    compare({}, 401, token=None)
    compare({}, 403, token=HEALTH)
    assert len(compare({})["rows"]) == 10
    seen = []
    arguments = {"limit": 7}
    for _ in range(25):
        result = compare(arguments)
        seen.extend(row["key"] for row in result["rows"])
        if result["next_cursor"] is None:
            break
        arguments["after_key"] = result["next_cursor"]
    else:
        raise AssertionError("Definition pages did not finish")
    assert len(seen) == len(set(seen)) and len(seen) > 90
    assert seen == sorted(seen)
    assert len(compare({"limit": 50})["rows"]) == 50
    for before in (None, 9, 8, 6, 2, 1, 2147483647, "6.0", True):
        result = compare({"definition_key": "user.z000", "before_version": before})
        assert result["rows"][0]["contract"]["version"] == 8
    for before in (2147483648, 9223372036854775807, 10**100):
        compare({"definition_key": "user.z000", "before_version": before}, 503)
        compare({"definition_key": "user.z001", "before_version": before}, 503)
        compare({"definition_key": "absent", "before_version": before})
    for key in ("user.z001", "user.a000", "absent", "", "Пример 😀"):
        compare({"definition_key": key})
    compare({"definition_key": "user.z000", "after_key": "user.z000"})
    for limit in (True, "17.0", "+17", "1_7"):
        compare({"limit": limit})
    for bad in (
        {"limit": 0},
        {"limit": 51},
        {"limit": 10**100},
        {"limit": 1e100},
        {"limit": 1.5},
        {"limit": None},
        {"before_version": 0},
        {"before_version": -1},
        {"before_version": 1.5},
        {"after_key": 3},
        {"definition_key": False},
        {"unknown": True},
    ):
        compare(bad, 422)
    print(
        f"Matched owner discovery for all {len(seen)} system/custom/draft/retired definitions and immutable version cursors"
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
            responses = []
            arguments = [
                {"after_key": "user.", "limit": 5},
                {"limit": 50},
                {"definition_key": "user.a000"},
                {"definition_key": "user.z000"},
                {"definition_key": "user.z000", "before_version": 7},
                {"definition_key": "user.z001"},
                {"limit": 0},
                {"before_version": 0},
                {"limit": 1e100},
            ]
            for value in arguments:
                response = document(await client.call_tool(NAME, value))
                if not response["isError"]:
                    assert (
                        json.loads(response["content"][0]["text"]) == response["structuredContent"]
                    )
                    response["content"][0]["text"] = response["structuredContent"]
                responses.append(response)
            return descriptor, responses


async def mcp_compare(binary, engine, root):
    environment = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    for phase in ("no_grant", "schema", "facts", "future", "health_only", "diary_only"):
        with Session(engine) as session, session.begin():
            categories = ["health"] if phase == "health_only" else ["diary"]
            session.execute(
                text(
                    "INSERT INTO app_state(key,value) VALUES ('preferences:onboarding',CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                ),
                {"value": json.dumps({"model_categories": categories})},
            )
            session.execute(text("DELETE FROM app_state WHERE key LIKE 'tracker-consent:%'"))
            if phase in {"schema", "facts", "future"}:
                for identity in (UUID(int=20000), UUID(int=20070)):
                    consent = TrackerShareConsent(
                        definition_id=identity,
                        destination_kind="model",
                        destination_instance_id="model:mcp:local",
                        categories={"facts"} if phase == "facts" else {"schema"},
                        granted_at=datetime(2099 if phase == "future" else 2020, 1, 1, tzinfo=UTC),
                    )
                    session.add(
                        AppState(
                            key=f"tracker-consent:{identity}:model:model:mcp:local",
                            value=consent.model_dump(mode="json"),
                        )
                    )
        expected = await capture(sys.executable, ["-c", PYTHON_SERVER], environment, root, "python")
        actual = await capture(str(binary), ["mcp"], environment, root, "native")
        assert expected == actual, (
            phase,
            expected[0] == actual[0],
            [r["isError"] for r in expected[1]],
            [r["isError"] for r in actual[1]],
        )
        if phase != "health_only":
            first = actual[1][0]["structuredContent"]
            keys = [row["key"] for row in first["rows"]]
            assert len(keys) == 5 and "user.z000" in keys
            history = actual[1][3]["structuredContent"]["rows"][0]
            assert [v["version"] for v in history["versions"]] == (
                [4, 5, 6, 7, 8] if phase == "schema" else [4, 6, 8]
            )
            assert history["versions_before"] == 4
            assert actual[1][5]["structuredContent"]["rows"] == []
    print(
        "Matched pinned MCP descriptor, pages beyond 70 hidden definitions, exact historical privacy, schema/facts/future/revoked grants and diary-only access"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    binary = parser.parse_args().binary.resolve(strict=True)
    engine = isolated_engine()
    try:
        seed(engine)
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
