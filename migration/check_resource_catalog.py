"""Compare versioned resource discovery, availability and model privacy with Python."""

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
from check_event_definitions import seed as seed_definitions
from check_mcp_stdio import PYTHON_SERVER, document
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.metric_definitions import (
    CoveragePolicy,
    MetricSpec,
    _upsert_metric_definition,
    ensure_system_metric_definitions,
)
from garmin_ai.models import (
    AppState,
    Event,
    EventMetricMapping,
    Measurement,
    MetricObservation,
    SourcePayload,
)
from garmin_ai.scenario_packs import ensure_scenario_packs
from garmin_ai.share_policy import TrackerShareConsent

NAME = "resource_catalog"
PATH = "/tools/" + NAME
AT = datetime(2026, 10, 25, 9, 0, 0, 123456, tzinfo=UTC)
TOKENS = [
    {
        "key": "synthetic-resource-both-reader-validation-only",
        "scopes": ["read:health", "read:diary"],
    },
    {"key": "synthetic-resource-health-reader-validation-only", "scopes": ["read:health"]},
    {"key": "synthetic-resource-diary-reader-validation-only", "scopes": ["read:diary"]},
]
BOTH, HEALTH, DIARY = [token["key"] for token in TOKENS]
KINDS = ["events", "system_metrics", "tracker_metrics", "raw_sources"]


def metric_spec(key, label):
    return MetricSpec(
        key=key,
        labels={"en": label, "ru": "Я" * 120},
        value_kind="physical_number",
        unit="count",
        dimension="count",
        aggregation="mean",
        allowed_methods={"mean", "max"},
        coverage=CoveragePolicy(kind="sparse"),
        time_semantics="point",
        minimum=0,
        maximum=100,
    )


def seed(engine):
    seed_definitions(engine)
    with Session(engine) as session, session.begin():
        session.execute(delete(SourcePayload))
        ensure_system_metric_definitions(session)
        ensure_scenario_packs(session, legacy_install=True)
        for index in range(80):
            version = _upsert_metric_definition(
                session,
                metric_spec(f"user.metric{index:03d}", f"Synthetic tracker metric {index}"),
                namespace="user",
            )
            if index != 71:
                event_version = UUID(int=30000 + index * 10 + (8 if index == 70 else 1))
                session.add(
                    EventMetricMapping(
                        event_definition_version_id=event_version,
                        field_id=f"synthetic.{index}",
                        metric_definition_version_id=version.id,
                    )
                )
            if index == 70:
                session.add(
                    EventMetricMapping(
                        event_definition_version_id=UUID(int=30001),
                        field_id="synthetic.historical",
                        metric_definition_version_id=version.id,
                    )
                )
                version = _upsert_metric_definition(
                    session,
                    metric_spec("user.metric070", "Synthetic revised metric"),
                    namespace="user",
                )
                session.add(
                    EventMetricMapping(
                        event_definition_version_id=UUID(int=30721),
                        field_id="synthetic.current",
                        metric_definition_version_id=version.id,
                    )
                )
            if index in {0, 70, 72}:
                for offset, valid in [(0, True), (1, True), (2, False)]:
                    session.add(
                        MetricObservation(
                            metric=f"user.metric{index:03d}",
                            value=offset,
                            unit="count",
                            metric_definition_version_id=version.id,
                            observed_at=AT + timedelta(seconds=offset),
                            source_calendar_date=AT.date(),
                            source_ref=UUID(int=40000 + index * 10 + offset),
                            fetched_at=AT,
                            timezone="UTC",
                            quality="observed",
                            valid=valid,
                            sequence=offset,
                            feature_version="source-plugin-v1" if offset == 2 else "synthetic-v1",
                        )
                    )
                for offset, quality in [(0, "observed"), (1, "missing")]:
                    session.add(
                        Measurement(
                            ts=AT + timedelta(seconds=offset),
                            metric=f"user.metric{index:03d}",
                            source="synthetic",
                            local_date=AT.date(),
                            value=2,
                            unit="count",
                            metric_definition_version_id=version.id,
                            quality=quality,
                            details={},
                        )
                    )
        for index in (0, 70, 72):
            for offset, status, deleted in [
                (0, "confirmed", False),
                (1, "pending", False),
                (2, "inferred", True),
            ]:
                session.add(
                    Event(
                        kind=f"user.a{index:03d}" if index < 70 else f"user.z{index - 70:03d}",
                        definition_version_id=UUID(
                            int=30000 + index * 10 + (8 if index == 70 else 1)
                        ),
                        start=AT + timedelta(seconds=offset),
                        timezone="UTC",
                        source="manual",
                        status=status,
                        deleted=deleted,
                        payload={"synthetic": True},
                    )
                )
        session.add(
            SourcePayload(
                source="plugin:source:synthetic:private",
                endpoint="source-record-v1",
                source_key="private-synthetic-identifier",
                payload_hash="synthetic-hash",
                payload={"secret": "synthetic-raw-value"},
                archive_key="inline:synthetic",
                fetched_at=AT,
                parser_version=1,
                status="raw_only",
            )
        )


def exercise(python, native):
    def compare(arguments, status=200, token=BOTH):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        replies = [
            client.post(PATH, json={"arguments": arguments}, headers=headers)
            for client in (python, native)
        ]
        assert all(reply.status_code == status for reply in replies), (
            arguments,
            status,
            [(r.status_code, r.text[:400]) for r in replies],
        )
        if status != 200:
            return None
        expected, actual = [reply.json() for reply in replies]
        assert expected == actual, (
            arguments,
            [(r["id"], r.get("availability")) for r in expected["resources"]][:3],
            [(r["id"], r.get("availability")) for r in actual["resources"]][:3],
        )
        return actual

    compare({"kind": "events"}, 401, token=None)
    for kind in KINDS:
        expected_token = DIARY if kind in {"events", "tracker_metrics"} else HEALTH
        forbidden_token = HEALTH if expected_token == DIARY else DIARY
        compare({"kind": kind}, 403, token=forbidden_token)
        compare({"kind": kind}, token=expected_token)
        seen = []
        args = {"kind": kind, "limit": 7}
        for _ in range(30):
            result = compare(args)
            seen.extend(row["id"] for row in result["resources"])
            if result["next_cursor"] is None:
                break
            args["after_key"] = result["next_cursor"]
        else:
            raise AssertionError("Catalog pages did not finish")
        assert seen == sorted(set(seen)), (kind, len(seen))
        compare({"kind": kind, "limit": 50})
        for key in seen[:2] + seen[-2:] + ["absent"]:
            compare({"kind": kind, "resource_key": key, "detail": True})
        for bad in (
            {"limit": 0},
            {"limit": 51},
            {"limit": 10**100},
            {"limit": 1e100},
            {"detail": True},
            {"after_key": "x", "resource_key": "y"},
            {"after_key": "x" * 201},
            {"detail": None},
            {"unknown": True},
        ):
            compare({"kind": kind, **bad}, 422)
        print("Matched paged", kind, "resources", len(seen))
    availability = compare(
        {"kind": "tracker_metrics", "resource_key": "user.metric072", "detail": True}
    )["resources"][0]["availability"]
    assert availability["canonical_observations"] == 2 and availability["measurements"] == 1
    assert availability["latest_at"] == (AT + timedelta(seconds=1)).isoformat()
    assert (
        compare({"kind": "tracker_metrics", "resource_key": "user.metric072"}, token=DIARY)[
            "resources"
        ][0]["availability"]
        is None
    )
    event = compare({"kind": "events", "resource_key": "user.z000"})["resources"][0]
    assert event["availability"]["records_for_version"] == 2
    raw = compare({"kind": "raw_sources"})
    assert "private-synthetic-identifier" not in str(raw) and "synthetic-raw-value" not in str(raw)
    for detail in ("true", 1, "off", 0):
        compare({"kind": "tracker_metrics", "resource_key": "user.metric070", "detail": detail})


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
            cases = [{"kind": kind, "limit": 50} for kind in KINDS] + [
                {"kind": "events", "resource_key": "user.a001", "detail": True},
                {"kind": "tracker_metrics", "resource_key": "user.metric000", "detail": True},
                {"kind": "tracker_metrics", "resource_key": "user.metric070", "detail": True},
                {"kind": "tracker_metrics", "resource_key": "user.metric072", "detail": True},
                {"kind": "tracker_metrics", "after_key": "user.", "limit": 3},
                {"kind": "events", "limit": 0},
                {"kind": "raw_sources", "detail": True},
                {"kind": "system_metrics", "unknown": True},
            ]
            for arguments in cases:
                response = document(await client.call_tool(NAME, arguments))
                if not response["isError"]:
                    assert (
                        json.loads(response["content"][0]["text"]) == response["structuredContent"]
                    )
                    response["content"][0]["text"] = response["structuredContent"]
                responses.append(response)
            return descriptor, responses


async def mcp_compare(binary, engine, root):
    env = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    for phase in (
        "no_grants",
        "schema",
        "facts",
        "both",
        "future",
        "health_only",
        "diary_only",
        "packs_off",
        "replay",
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
            session.execute(text("DELETE FROM app_state WHERE key LIKE 'tracker-consent:%'"))
            session.execute(
                text("UPDATE module_configs SET llm_enabled=:enabled"),
                {"enabled": phase != "packs_off"},
            )
            if phase in {"schema", "facts", "both", "future", "diary_only"}:
                for identity in (UUID(int=20000), UUID(int=20001)):
                    consent = TrackerShareConsent(
                        definition_id=identity,
                        destination_kind="model",
                        destination_instance_id="model:mcp:local",
                        categories={"schema", "facts"}
                        if phase in {"both", "diary_only"}
                        else {"facts"}
                        if phase == "facts"
                        else {"schema"},
                        granted_at=datetime(2099 if phase == "future" else 2020, 1, 1, tzinfo=UTC),
                    )
                    session.add(
                        AppState(
                            key=f"tracker-consent:{identity}:model:model:mcp:local",
                            value=consent.model_dump(mode="json"),
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
                        fetched_at=AT,
                        parser_version=0,
                        status="pending",
                        payload={"totalSteps": 0},
                    )
                )
        expected = await capture(sys.executable, ["-c", PYTHON_SERVER], env, root, "python")
        actual = await capture(str(binary), ["mcp"], env, root, "native")
        assert expected == actual, (
            phase,
            expected[0] == actual[0],
            [r["isError"] for r in expected[1]],
            [r["isError"] for r in actual[1]],
        )
        if phase == "schema":
            assert actual[1][5]["structuredContent"]["resources"][0]["availability"] is None
        if phase == "both":
            assert (
                actual[1][5]["structuredContent"]["resources"][0]["availability"][
                    "canonical_observations"
                ]
                == 2
            )
        if phase == "diary_only":
            assert actual[1][7]["structuredContent"]["resources"][0]["availability"] is None
        if phase == "replay":
            assert [r["isError"] for r in actual[1][:4]] == [False, True, False, True]
    print(
        "Matched pinned MCP descriptors, all resource families, historical mapping privacy, source-health suppression, category/pack grants and Garmin replay behavior"
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
