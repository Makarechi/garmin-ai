"""Check exact universal aggregate, projection identity and canon validation fixtures."""

import argparse
import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from check_custom_entries import configure_metric, entry, specification
from check_mcp_custom_diary import record
from check_mcp_diary_create import open_client
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner
from garmin_ai.canonical_events import backfill_canonical_events
from garmin_ai.definitions import activate_definition, create_definition_draft
from garmin_ai.events import EventInput, create_event
from garmin_ai.metric_definitions import (
    CoveragePolicy,
    MetricSpec,
    aggregate_metric,
    record_observation,
    register_metric_definition,
)
from garmin_ai.models import Event

AT = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def check_aggregate(engine, binary, env):
    with Session(engine) as session, session.begin():
        steps = register_metric_definition(
            session,
            MetricSpec(
                key="user.walking.steps",
                labels={"en": "Steps"},
                value_kind="increment",
                unit="steps",
                dimension="count",
                aggregation="sum",
                allowed_methods={"sum"},
                coverage=CoveragePolicy(kind="all_values"),
                time_semantics="interval",
                minimum=0,
                maximum=1_000_000,
            ),
            authorized=True,
        )
        for index, value in enumerate((100, 250)):
            start = AT + timedelta(minutes=index * 15)
            record_observation(
                session,
                steps,
                value,
                observed_at=start,
                effective_start=start,
                effective_end=start + timedelta(minutes=15),
                source_ref=uuid4(),
                ingested_at=AT + timedelta(hours=2),
            )
        expect = aggregate_metric(session, "user.walking.steps", AT, AT + timedelta(hours=1))
        assert expect["value"] == 350 and expect["coverage_ratio"] is None

    token = "synthetic-metric-only-administrator"
    args = {
        "operation": "aggregate_metric",
        "metric_key": "user.walking.steps",
        "start": AT.isoformat(),
        "end": (AT + timedelta(hours=1)).isoformat(),
        "method": "sum",
        "knowledge_cutoff": (AT + timedelta(hours=3)).isoformat(),
    }
    with subprocess.Popen(
        [binary, "serve", "--port", "0"],
        env={**env, "GA_API_TOKENS": json.dumps([{"key": token, "scopes": ["admin"]}])},
        stdout=subprocess.PIPE,
        text=True,
    ) as server:
        try:
            address = server.stdout.readline().strip().removeprefix("Listening on ")
            assert address.startswith("http://127.0.0.1:")
            response = httpx.post(
                address + "/tools/generic_analysis",
                headers={"Authorization": "Bearer " + token},
                json={"arguments": {"spec": args}},
                timeout=5,
            )
            assert response.status_code == 200, response.text
            actual = response.json()
            assert actual["value"] == expect["value"] and actual["coverage_ratio"] is None
        finally:
            server.terminate()
            server.wait(timeout=5)
    print("UT-11 native aggregate returned 350 with no time-weighted coverage")


async def check_same_timestamp(engine, binary, env):
    with Session(engine) as session, session.begin():
        draft = create_definition_draft(
            session, specification(10, True), actor="synthetic", authorized=True
        )
        definition = activate_definition(
            session, draft.id, draft.revision, actor="synthetic", authorized=True
        )
        configure_metric(session, definition, 10)

    identifiers = set()
    with TemporaryDirectory() as directory:
        async with open_client(
            str(binary), ["mcp"], Path(directory), {**env, "GA_MCP_ENABLE_WRITES": "true"}, "native"
        ) as native:
            for value in (3, 4):
                result = await record(native, f"synthetic-same-time-{value}", entry(value))
                assert not result.get("isError"), result
                identifiers.add(result["structuredContent"]["id"])
    with engine.connect() as connection:
        rows = connection.execute(
            text("""
            SELECT observation.id::text, observation.source_entry_id::text,
                   observation.metric_definition_version_id::text, events.start
              FROM metric_observations observation JOIN events ON events.id=observation.source_entry_id
             WHERE observation.valid
        """)
        ).all()
    assert len(rows) == 2 and {row[1] for row in rows} == identifiers
    assert len({row[0] for row in rows}) == 2 and len({row[2] for row in rows}) == 1
    assert len({row[3] for row in rows}) == 1
    print(
        "UT-13 two native same-time records retained distinct projected facts and the same version"
    )


def check_backfill(engine, binary, env):
    with Session(engine) as session, session.begin():
        create_event(
            session,
            EventInput(
                start=AT, payload={"type": "note", "description": "Synthetic canonical event"}
            ),
            actor="fixture",
        )
        assert backfill_canonical_events(session) == backfill_canonical_events(session) == 3
    count = text("SELECT count(*) FROM audit_log")
    with engine.connect() as connection:
        before = connection.scalar(count)
    for _ in range(2):
        result = subprocess.run(
            [binary, "migrate"], env=env, text=True, capture_output=True, timeout=12
        )
        assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        assert connection.scalar(count) == before
    with Session(engine) as session, session.begin():
        session.add(
            Event(
                kind="symptom_observation",
                start=AT,
                timezone="UTC",
                source="manual",
                payload={"type": "symptom_observation", "impact": ""},
                topology="point",
            )
        )
    with Session(engine) as session:
        try:
            backfill_canonical_events(session)
        except ValueError as error:
            assert "unresolved definitions" in str(error)
        else:
            raise AssertionError("Python accepted an invalid unbound definition")
    failure = subprocess.run(
        [binary, "migrate"], env=env, text=True, capture_output=True, timeout=12
    )
    assert failure.returncode == 1 and "initialized safely" in failure.stderr, failure.stderr
    with engine.connect() as connection:
        assert connection.scalar(count) == before
    print(
        "UT-16 native initialization preserved audit rows on repetition and refused an unresolved definition"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    engine = isolated_engine()
    try:
        env = {"GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"]}
        with Session(engine) as session, session.begin():
            owner(session)
        check_aggregate(engine, binary, env)
        asyncio.run(check_same_timestamp(engine, binary, env))
        check_backfill(engine, binary, env)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
