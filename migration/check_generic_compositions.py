"""Compare generic period and completeness results with the frozen application."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_generic_aggregate_history import capture
from check_generic_aggregate_history import cases as aggregate_cases
from check_generic_observation_history import seed as seed_observations
from generic_aggregate_fixtures import add_overflow, configure_phase
from generic_aggregate_fixtures import cases as edge_cases
from generic_aggregate_fixtures import request as edge_request
from generic_aggregate_fixtures import seed as seed_edges
from generic_composition_fixtures import cases as paired_cases
from generic_composition_fixtures import seed as seed_pairs
from sqlalchemy import text


def composed_cases(owner):
    return compose(aggregate_cases(owner))


def compose(requests):
    results = []
    for original in requests:
        for operation in ["compare_periods", "query_completeness"]:
            case = {
                key: value
                for key, value in original.items()
                if key not in {"expected_value", "row_count"}
            }
            case.update(
                name=operation + "-" + original["name"],
                spec={**original["spec"], "operation": operation},
            )
            if operation == "compare_periods":
                case["spec"].update(
                    comparison_start=case["spec"]["start"], comparison_end=case["spec"]["end"]
                )
            results.append(case)
    return results


def compare(binary, engine, fixture, requests, label):
    values = capture(engine, requests)
    for value in values:
        if value["spec"]["operation"] == "query_completeness" and "expected" in value:
            assert value["expected"]["reporting_completeness"] == "unknown"
            assert value["expected"]["complete"] is None
    fixture.write_text(json.dumps(values, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    subprocess.run(
        [binary, "--ignored", "--exact", "compares_generic_composition_history"],
        check=True,
        env={**os.environ, "GA_TEST_GENERIC_COMPOSITIONS": str(fixture)},
    )
    print("Matched generic compositions", label, "cases", len(values), flush=True)


def run(binary):
    engine = isolated_engine()
    try:
        owner = seed_observations(engine)
        seed_pairs(engine, owner)
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "compositions.json"
            compare(
                binary, engine, fixture, paired_cases(owner), "paired windows and calendar bounds"
            )
            compare(binary, engine, fixture, composed_cases(owner), "sources and evidence")
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
                compare(binary, engine, fixture, composed_cases(owner), phase)
            configure_phase(engine, "facts")
            seed_edges(engine, owner)
            bounded = [
                case for case in edge_cases(owner) if "10000-full-categories" not in case["name"]
            ]
            compare(
                binary,
                engine,
                fixture,
                compose(bounded),
                "history, bounds and known-state evidence",
            )
            add_overflow(engine)
            boundaries = [
                {
                    "name": "observation-bound",
                    "spec": edge_request("bound_metric", source="observation:[null,null]"),
                },
                {"name": "multiple-before-bound", "spec": edge_request("bound_metric")},
                {
                    "name": "recovered-filter",
                    "spec": edge_request("bound_metric", source='observation:["unique",null]'),
                },
            ]
            boundaries += [
                {**case, "name": "mcp-" + case["name"], "owner": str(owner)} for case in boundaries
            ]
            compare(binary, engine, fixture, compose(boundaries), "overflow and recovery")
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
                    compare(binary, engine, fixture, composed_cases(owner), zone)
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
