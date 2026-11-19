"""Compare native HTTP/MCP timelines, recorded-version privacy and large overlaps."""

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
from check_mcp_stdio import document
from check_timeline import START
from check_timeline import seed as seed_timeline
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.models import (
    AppState,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    Person,
    SourcePayload,
)
from garmin_ai.scenario_packs import ensure_scenario_packs
from garmin_ai.share_policy import TrackerShareConsent

NAME = "timeline"
TOKEN = "synthetic-timeline-both-reader-validation-only"
BASIC = {"start": START.isoformat(), "end": (START + timedelta(hours=1)).isoformat()}
TRACKER = UUID(int=70000)
FUTURE = UUID(int=70001)
PRIVATE = UUID(int=70002)
SENSITIVE = UUID(int=70003)
HIDDEN = UUID(int=70004)


def seed(engine):
    seed_timeline(engine)
    with Session(engine) as session, session.begin():
        ensure_scenario_packs(session, legacy_install=True)
        owner = session.scalar(select(Person.id))
        session.add_all(
            [
                EventDefinition(
                    id=TRACKER,
                    owner_id=owner,
                    namespace="user",
                    key="user.timeline",
                    status="retired",
                    current_version=2,
                ),
                EventDefinition(
                    id=FUTURE,
                    owner_id=owner,
                    namespace="user",
                    key="user.hidden_timeline",
                    status="active",
                    current_version=1,
                ),
            ]
        )
        session.flush()
        for identity, definition, version, privacy in [
            (PRIVATE, TRACKER, 1, "private"),
            (SENSITIVE, TRACKER, 2, "sensitive"),
            (HIDDEN, FUTURE, 1, "sensitive"),
        ]:
            session.add(
                EventDefinitionVersion(
                    id=identity,
                    definition_id=definition,
                    version=version,
                    schema={},
                    schema_hash="0" * 64,
                    topology="point",
                    field_metadata={},
                    labels={"en": "Synthetic timeline"},
                    privacy=privacy,
                    allowed_operations=["query", "update"],
                )
            )
        session.flush()
        for index, version in enumerate((PRIVATE, SENSITIVE)):
            session.add(
                Event(
                    id=UUID(int=71000 + index),
                    kind="user.timeline",
                    definition_version_id=version,
                    start=START + timedelta(seconds=index + 1),
                    timezone="UTC",
                    source="manual",
                    original_text="Synthetic private quote",
                    payload={"description": f"Synthetic version {index + 1}"},
                )
            )
        session.execute(
            text("""
            INSERT INTO events(id,kind,start,timezone,source,confidence,status,original_text,
                payload,topology,envelope_version,time_precision,assertion_kind,producer,
                evidence_refs,validation_status,revision,deleted,definition_version_id)
            SELECT gen_random_uuid(),'user.hidden_timeline',:start,'UTC','manual',1,'confirmed',
                'Synthetic hidden quote',jsonb_build_object('description','Synthetic hidden timeline'),
                'point',1,'instant','user_report','owner','[]','schema_validated',1,false,:version
            FROM generate_series(1,1200)
        """),
            {"start": START, "version": HIDDEN},
        )


async def capture(command, args, env, root, label, cases):
    params = StdioServerParameters(command=command, args=args, env=env, cwd=root)
    with (root / f"{label}.log").open("w") as errors:
        async with (
            stdio_client(params, errlog=errors) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            await client.initialize()
            catalog = [
                document(tool)
                for tool in (await client.list_tools()).tools
                if tool.name != "generic_analysis"
            ]
            assert len(catalog) == 22
            descriptor = catalog
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


async def verify(binary, engine, root):
    env = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    cases = [
        BASIC,
        {"start": START.isoformat(), "end": START.isoformat()},
        {**BASIC, "unknown": True},
        {
            "start": (START + timedelta(days=2)).isoformat(),
            "end": (START + timedelta(days=2, minutes=1)).isoformat(),
        },
        {
            "start": (START + timedelta(days=2)).isoformat(),
            "end": (START + timedelta(days=2, minutes=2)).isoformat(),
        },
    ]
    for phase in (
        "no_grant",
        "schema",
        "facts",
        "future",
        "future_hidden",
        "health_only",
        "diary_only",
        "migraine_off",
        "sleep_off",
        "replay",
    ):
        with Session(engine) as session, session.begin():
            session.execute(
                text(
                    "INSERT INTO app_state(key,value) VALUES ('preferences:onboarding',CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                ),
                {
                    "value": json.dumps(
                        {
                            "model_categories": ["health"]
                            if phase == "health_only"
                            else ["diary"]
                            if phase == "diary_only"
                            else ["health", "diary"]
                        }
                    )
                },
            )
            session.execute(text("DELETE FROM app_state WHERE key LIKE 'tracker-consent:%'"))
            session.execute(text("UPDATE module_configs SET llm_enabled=true"))
            if phase.endswith("_off"):
                session.execute(
                    text("UPDATE module_configs SET llm_enabled=false WHERE pack_key=:pack"),
                    {"pack": phase.removesuffix("_off")},
                )
            if phase in {"schema", "facts", "future", "future_hidden"}:
                grant = TrackerShareConsent(
                    definition_id=FUTURE if phase == "future_hidden" else TRACKER,
                    destination_kind="model",
                    destination_instance_id="model:mcp:local",
                    categories={"schema"} if phase == "schema" else {"facts"},
                    granted_at=datetime(
                        2099 if phase in {"future", "future_hidden"} else 2020, 1, 1, tzinfo=UTC
                    ),
                )
                session.add(
                    AppState(
                        key=f"tracker-consent:{grant.definition_id}:model:model:mcp:local",
                        value=grant.model_dump(mode="json"),
                    )
                )
            if phase == "replay":
                session.add(
                    SourcePayload(
                        source="garmin_connect",
                        endpoint="daily",
                        source_key="2026-10-25",
                        payload_hash="b" * 64,
                        archive_key="sha256:" + "b" * 64,
                        fetched_at=START,
                        parser_version=0,
                        status="pending",
                        payload={"totalSteps": 0},
                    )
                )
        expected = await capture(
            sys.executable, ["-m", "garmin_ai.mcp_server"], env, root, "python", cases
        )
        actual = await capture(str(binary), ["mcp"], env, root, "native", cases)
        assert expected == actual, (
            phase,
            expected[0] == actual[0],
            [r["isError"] for r in expected[1]],
            [r["isError"] for r in actual[1]],
        )
        if phase in {"no_grant", "schema", "facts", "future", "migraine_off"}:
            value = actual[1][0]["structuredContent"]
            rows = value["events"]["rows"]
            assert not any("original_text" in row for row in rows)
            assert not any(row["kind"] == "user.hidden_timeline" for row in rows)
            assert len([r for r in rows if r["kind"] == "user.timeline"]) == (
                2 if phase == "facts" else 1
            )
            if phase == "migraine_off":
                assert not any(row["kind"] == "migraine" for row in rows)
    print(
        "Matched actual Python MCP timeline descriptor, all layers/segments, old-version tracker facts, 1200 hidden rows, grants, categories, packs, 500 annotations and replay denial"
    )


def http_large(binary, engine):
    with Session(engine) as session, session.begin():
        session.execute(
            text("DELETE FROM source_payloads WHERE source='garmin_connect' AND endpoint='daily'")
        )
    settings = Settings(
        _env_file=None, api_tokens=[ApiToken(key=TOKEN, scopes=["read:health", "read:diary"])]
    )
    with (
        TestClient(create_app(settings, engine)) as python,
        subprocess.Popen(
            [binary, "serve", "--port", "0"],
            env={
                "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                "GA_API_TOKENS": json.dumps(
                    [{"key": TOKEN, "scopes": ["read:health", "read:diary"]}]
                ),
            },
            stdout=subprocess.PIPE,
            text=True,
        ) as server,
    ):
        try:
            url = server.stdout.readline().strip().removeprefix("Listening on ")
            assert url.startswith("http://127.0.0.1:")
            with httpx.Client(base_url=url, timeout=120, trust_env=False) as native:
                for day in (4, 5):
                    arguments = {
                        "start": (START + timedelta(days=day)).isoformat(),
                        "end": (START + timedelta(days=day, hours=1)).isoformat(),
                    }
                    replies = [
                        client.post(
                            "/tools/timeline",
                            json={"arguments": arguments},
                            headers={"Authorization": f"Bearer {TOKEN}"},
                        )
                        for client in (python, native)
                    ]
                    assert all(r.status_code == 200 for r in replies), [
                        (r.status_code, r.text[:100]) for r in replies
                    ]
                    assert replies[0].json() == replies[1].json(), day
                    if day == 4:
                        assert len(replies[1].content) > 16 * 1024 * 1024
                    print(
                        "Matched complete HTTP timeline for dense/large evidence day",
                        day,
                        "bytes",
                        len(replies[1].content),
                    )
        finally:
            server.terminate()
            server.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    binary = parser.parse_args().binary.resolve(strict=True)
    engine = isolated_engine()
    try:
        seed(engine)
        with TemporaryDirectory() as directory:
            asyncio.run(verify(binary, engine, Path(directory)))
        http_large(binary, engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
