"""Compare native generic aggregate results and evidence with frozen Python."""

import argparse
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_metric_aggregates import (
    CUTOFF,
    END,
    START,
)
from capture_metric_aggregates import (
    capture as seed_calculation,
)
from capture_metric_aggregates import (
    requests as calculation_requests,
)
from capture_reference import isolated_engine
from check_generic_entry_history import T0, T1, T2, T3, VERSION1
from check_generic_observation_history import (
    arguments as observation_arguments,
)
from check_generic_observation_history import (
    seed as seed_observations,
)
from generic_aggregate_fixtures import add_overflow, configure_phase
from generic_aggregate_fixtures import cases as edge_cases
from generic_aggregate_fixtures import request as edge_request
from generic_aggregate_fixtures import seed as seed_edges
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from garmin_ai.models import EventMetricMapping
from garmin_ai.source_metric_projection import has_source_observations
from garmin_ai.tools import call_tool


def capture(engine, requests):
    results = []
    for case in requests:
        with Session(engine) as session, session.begin():
            if case.get("owner"):
                session.info.update(llm_access=True, model_provider_instance_id="model:mcp:local")
            try:
                if not case.get("owner") and not case.get("read_health", True):
                    session.execute(select(func.pg_advisory_xact_lock_shared(72104619)))
                    metric = case["spec"].get("metric_key")
                    if metric and has_source_observations(session, metric):
                        raise PermissionError("Insufficient scope")
                expected = call_tool(session, "generic_analysis", {"spec": case["spec"]})
                try:
                    json.dumps(expected, allow_nan=False)
                except ValueError:
                    results.append({**case, "error": "ValueError", "error_phase": "json"})
                else:
                    results.append({**case, "expected": expected})
            except Exception as error:
                value = {**case, "error": type(error).__name__}
                if type(error) in (ValueError, LookupError):
                    value["error_message"] = str(error)
                results.append(value)
    return results


def compare(binary, engine, fixture, requests, label):
    values = capture(engine, requests)
    for value in values:
        if value["name"].endswith(("nuls-source", "unpaired-source")):
            assert "error" in value, value["name"]
        if "expected_value" in value:
            assert "expected" in value, value
            assert value["expected"]["value"] == value["expected_value"], value["name"]
        if "row_count" in value:
            assert "expected" in value, value
            assert len(value["expected"]["rows"]) == value["row_count"], value["name"]
    fixture.write_text(json.dumps(values, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    subprocess.run(
        [binary, "--ignored", "--exact", "compares_generic_aggregate_history"],
        check=True,
        env={**os.environ, "GA_TEST_GENERIC_AGGREGATES": str(fixture)},
    )
    print("Matched generic aggregate", label, "cases", len(values), flush=True)


def arguments(**changes):
    return observation_arguments(operation="aggregate_metric", **changes)


def cases(owner):
    result = []
    for source in [
        None,
        "event",
        "observation:[null,null]",
        'observation:["",""]',
        'observation:["synthetic-provider",null]',
        "measurement:legacy",
        "measurement:corrected",
        "measurement:same-clock",
        "measurement:changed-contract",
        "measurement:changed-quality",
        "measurement:legacy-with-future-revision",
        "measurement:legacy-future",
        "measurement:legacy-unknown",
        "measurement:microseconds",
        "measurement:Unicode Я😀",
        "measurement:absent",
    ]:
        for index, cutoff in enumerate([T0, T1, T2, T3]):
            result.append(
                {
                    "name": f"{source}-{index}",
                    "spec": arguments(source=source, knowledge_cutoff=cutoff.isoformat()),
                }
            )
    for index, method in enumerate(
        [
            None,
            "sum",
            "latest",
            "min",
            "max",
            "median",
            "distribution",
            "mode",
            "rate",
            "count_true",
            "delta",
            "counts",
        ]
    ):
        result.append(
            {
                "name": f"method-{index}",
                "spec": arguments(source="observation:[null,null]", method=method),
            }
        )
    result += [
        {
            "name": "limited-source-owner",
            "read_health": False,
            "spec": arguments(source="measurement:legacy"),
        },
        {"name": "old-version", "spec": arguments(source="event", metric_version=1)},
        {"name": "absent-version", "spec": arguments(metric_version=3)},
        {"name": "overflow-version", "spec": arguments(metric_version=2147483648)},
        {"name": "missing-metric", "spec": arguments(metric_key="user.absent")},
        {"name": "system-metric", "spec": arguments(metric_key="system.steps_bucket")},
        {
            "name": "unused-definition",
            "spec": arguments(source="measurement:legacy", definition_key="user.absent"),
        },
        {
            "name": "nuls-source",
            "spec": arguments(source="observation:" + json.dumps([chr(0), None])),
        },
        {
            "name": "unpaired-source",
            "spec": arguments(source="observation:" + json.dumps([chr(0xD800), None])),
        },
        {"name": "first-cutoff", "spec": arguments(knowledge_cutoff="0001-01-01T00:00:00+01:00")},
        {"name": "last-cutoff", "spec": arguments(knowledge_cutoff="9999-12-31T23:59:59-01:00")},
    ]
    return result + [
        {**case, "name": "mcp-" + case["name"], "owner": str(owner)} for case in result
    ]


def run(binary):
    engine = isolated_engine()
    try:
        owner = seed_observations(engine)
        requests = []
        with Session(engine) as session, session.begin():
            for index, request in enumerate(calculation_requests()):
                seed_calculation(session, index, request, owner)
                session.add(
                    EventMetricMapping(
                        event_definition_version_id=VERSION1,
                        metric_definition_version_id=UUID(int=1000001 + index * 2),
                        field_id=f"aggregate_{index}",
                    )
                )
                requests.append(
                    {
                        "name": f"calculation-{index}",
                        "spec": {
                            "operation": "aggregate_metric",
                            "metric_key": f"user.synthetic_{index}",
                            "method": request["method"],
                            "start": START.isoformat(),
                            "end": END.isoformat(),
                            "knowledge_cutoff": CUTOFF.isoformat(),
                        },
                    }
                )
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "aggregates.json"
            compare(
                binary,
                engine,
                fixture,
                requests
                + [
                    {**case, "name": "mcp-" + case["name"], "owner": str(owner)}
                    for case in requests
                ],
                "all value kinds and methods",
            )
            compare(binary, engine, fixture, cases(owner), "source and historical evidence")
            for phase in [
                "no_grant",
                "schema",
                "facts",
                "health_only",
                "diary_only",
                "activity_off",
                "replay",
            ]:
                configure_phase(engine, phase)
                compare(binary, engine, fixture, cases(owner), phase)
            configure_phase(engine, "facts")
            seed_edges(engine, owner)
            compare(
                binary, engine, fixture, edge_cases(owner), "legacy history and interval boundaries"
            )
            add_overflow(engine)
            limits = [
                {
                    "name": "10001-observations",
                    "spec": edge_request("bound_metric", source="observation:[null,null]"),
                },
                {"name": "multiple-before-bound", "spec": edge_request("bound_metric")},
                {
                    "name": "filter-before-bound",
                    "expected_value": 2.0,
                    "spec": edge_request("bound_metric", source='observation:["unique",null]'),
                },
                {
                    "name": "still-10000",
                    "expected_value": 10000.0,
                    "spec": edge_request(
                        "bound_metric",
                        source="observation:[null,null]",
                        end=(START + timedelta(microseconds=1)).isoformat(),
                    ),
                },
                {
                    "name": "empty-event-recovery",
                    "expected_value": None,
                    "spec": edge_request("bound_metric", source="event"),
                },
            ]
            compare(
                binary,
                engine,
                fixture,
                limits
                + [{**case, "name": "mcp-" + case["name"], "owner": str(owner)} for case in limits],
                "bounds, filtering and recovery",
            )
            database_name = engine.dialect.identifier_preparer.quote(engine.url.database)
            try:
                for zone in ["America/New_York", "Asia/Kathmandu"]:
                    with engine.connect().execution_options(
                        isolation_level="AUTOCOMMIT"
                    ) as connection:
                        connection.execute(
                            text(f"ALTER DATABASE {database_name} SET timezone TO '{zone}'")
                        )
                    engine.dispose()
                    compare(binary, engine, fixture, cases(owner), zone)
            finally:
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                    connection.execute(text(f"ALTER DATABASE {database_name} RESET timezone"))
                engine.dispose()
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
