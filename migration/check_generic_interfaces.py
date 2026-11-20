"""Compare generic analysis through the normal HTTP and MCP entry points."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from capture_analysis_specs import arguments as spec_arguments
from capture_analysis_specs import cases as spec_cases
from capture_reference import isolated_engine
from check_generic_aggregate_history import cases as aggregate_cases
from check_generic_entry_history import AT, T0, T3
from check_generic_observation_history import seed as seed_observations
from check_mcp_stdio import PYTHON_SERVER, document
from fastapi.testclient import TestClient
from generic_aggregate_fixtures import configure_phase
from generic_composition_fixtures import cases as paired_cases
from generic_composition_fixtures import seed as seed_pairs
from generic_threshold_fixtures import cases as threshold_cases
from generic_threshold_fixtures import seed as seed_thresholds
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.generic_analytics import AnalysisSpec

TOKENS = [
    {"key": "synthetic-generic-both-transport-proof", "scopes": ["read:health", "read:diary"]},
    {"key": "synthetic-generic-health-transport-proof", "scopes": ["read:health"]},
    {"key": "synthetic-generic-diary-transport-proof", "scopes": ["read:diary"]},
    {"key": "synthetic-generic-admin-transport-proof", "scopes": ["admin"]},
    {"key": "synthetic-generic-write-transport-proof", "scopes": ["write:diary"]},
]
NAME = "generic_analysis"


def requests(owner, sources):
    result = [
        case
        for case in aggregate_cases(owner) + paired_cases(owner) + threshold_cases(owner, sources)
        if not case.get("owner") and case.get("read_health", True) and "random-" not in case["name"]
    ]
    result = [
        case for case in result if case["spec"].get("method") in {None, "mean", "sum", "latest"}
    ]
    for operation in ["query_observations", "query_completeness"]:
        for original in aggregate_cases(owner)[:16]:
            result.append(
                {
                    "name": operation + original["name"],
                    "spec": {**original["spec"], "operation": operation},
                }
            )
    result.append(
        {
            "name": "entries",
            "spec": {
                "operation": "query_entries",
                "definition_key": "user.history",
                "start": AT.isoformat(),
                "end": T3.isoformat(),
                "knowledge_cutoff": T3.isoformat(),
            },
        }
    )
    result.append(
        {
            "name": "entries-before-invalid-audit",
            "spec": {**result[-1]["spec"], "knowledge_cutoff": T0.isoformat()},
        }
    )
    return [{"name": case["name"], "arguments": {"spec": case["spec"]}} for case in result]


def response_value(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def http_compare(python, native, requests, label):
    count = 0
    for token in TOKENS:
        headers = {"Authorization": "Bearer " + token["key"]}
        expected = python.get("/tools", headers=headers)
        actual = native.get("/tools", headers=headers)
        assert expected.status_code == actual.status_code and response_value(
            expected
        ) == response_value(actual), "HTTP catalog " + label
        if expected.status_code == 200 and token["scopes"] == ["read:health", "read:diary"]:
            assert len(actual.json()) == 23
        for case in requests:
            expected = python.post(
                "/tools/" + NAME, json={"arguments": case["arguments"]}, headers=headers
            )
            actual = native.post(
                "/tools/" + NAME, json={"arguments": case["arguments"]}, headers=headers
            )
            assert expected.status_code == actual.status_code, (
                label,
                token["scopes"],
                case["name"],
                expected.status_code,
                actual.status_code,
            )
            assert expected.headers.get("retry-after") == actual.headers.get("retry-after"), (
                label,
                case["name"],
                "retry-after",
            )
            assert response_value(expected) == response_value(actual), (
                label,
                token["scopes"],
                case["name"],
                response_value(expected),
                response_value(actual),
            )
            count += 1
    print("Matched generic HTTP", label, "requests", count, flush=True)


def validation_cases():
    for arguments in ({}, {"spec": None}, {"spec": []}, {"spec": {}}, {"extra": True}):
        yield {"arguments": arguments}
    base = spec_arguments("query_observations")
    for overrides in (
        {"source": False, "method": "X", "limit": 0, "start": "bad", "other": 1},
        {"start": "2026-02-29", "end": "2026-01-01T00:00:00+30:00"},
        {"start": "0000-01-01T00:00:00Z", "comparison_start": 1e20},
        {"start": "0000-01-01", "end": "2026-01-01"},
        {"limit": 1e30, "metric_version": "9" * 4301},
        {"limit": "a" * 4301, "metric_version": "9" * 4300 + "x"},
        {"limit": "0" * 4301, "metric_version": "-" + "9" * 4300},
        {"limit": "9" + "_" * 4300 + "0", "metric_version": "9" * 4301 + "x"},
    ):
        yield {"arguments": {"spec": base | overrides, "z_extra": 1, "a_extra": 2}}
    yield {"arguments": {"spec": base, "extra": "synthetic"}}
    specs = {json.dumps(spec, ensure_ascii=True): spec for spec in spec_cases()}.values()
    for spec in specs:
        try:
            AnalysisSpec.model_validate(spec)
        except (OverflowError, ValueError):
            yield {"arguments": {"spec": spec}}
    threshold = spec_arguments("threshold_segments")
    for value in (float("nan"), float("inf"), float("-inf")):
        yield {"arguments": {"spec": {**threshold, "threshold": value}}}


def http_validation_compare(python, native):
    headers = {
        "Authorization": "Bearer " + TOKENS[0]["key"],
        "Content-Type": "application/json",
    }
    mismatches = {}
    count = 0
    for body in validation_cases():
        raw = json.dumps(body, ensure_ascii=True)
        before = python.post("/tools/" + NAME, content=raw, headers=headers)
        after = native.post("/tools/" + NAME, content=raw, headers=headers)
        count += 1
        if before.status_code == after.status_code and response_value(before) == response_value(
            after
        ):
            continue
        expected = response_value(before)
        actual = response_value(after)
        if isinstance(expected, dict) and isinstance(expected.get("detail"), list):
            fields = ",".join(
                ".".join(map(str, row["loc"])) + ":" + row["type"] for row in expected["detail"]
            )
            sample = [
                {key: value for key, value in row.items() if key != "input"}
                for row in expected["detail"]
            ]
        else:
            fields = str(expected)
            sample = expected
        key = (before.status_code, after.status_code, fields)
        mismatches.setdefault(key, {"sample": sample, "actual": actual, "count": 0})
        mismatches[key]["count"] += 1
    for key, value in mismatches.items():
        print("Validation mismatch", key, value, flush=True)
    assert not mismatches, (count, len(mismatches))
    print("Matched generic HTTP typed validation requests", count, flush=True)


async def mcp_capture(command, args, environment, root, cases, label):
    process = StdioServerParameters(command=command, args=args, env=environment, cwd=root)
    with (root / (label + ".log")).open("w") as errors:
        async with (
            stdio_client(process, errlog=errors) as (incoming, outgoing),
            ClientSession(incoming, outgoing) as client,
        ):
            await client.initialize()
            tools = [document(tool) for tool in (await client.list_tools()).tools]
            assert len(tools) == 23
            responses = []
            for case in cases:
                response = document(await client.call_tool(NAME, case["arguments"]))
                if not response["isError"]:
                    assert (
                        json.loads(response["content"][0]["text"]) == response["structuredContent"]
                    )
                    response["content"][0]["text"] = response["structuredContent"]
                responses.append(response)
            return tools, responses


async def mcp_compare(binary, engine, root, cases):
    environment = {"GA_DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    invalid = [
        {"name": "invalid-" + str(index), "arguments": arguments}
        for index, arguments in enumerate(
            [{}, {"spec": None}, {"spec": {}}, {"spec": [], "extra": 1}]
        )
    ]
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
        selected = cases + invalid
        if phase in {"facts", "replay"}:
            unique = {
                json.dumps(spec, sort_keys=True, ensure_ascii=True): spec for spec in spec_cases()
            }
            selected += [
                {"name": "typed-spec-" + str(index), "arguments": {"spec": spec}}
                for index, spec in enumerate(unique.values())
            ]
        expected = await mcp_capture(
            sys.executable, ["-c", PYTHON_SERVER], environment, root, selected, "python"
        )
        actual = await mcp_capture(str(binary), ["mcp"], environment, root, selected, "native")
        assert expected[0] == actual[0], "Full MCP catalog differs"
        for case, expected_response, actual_response in zip(
            selected, expected[1], actual[1], strict=True
        ):
            assert expected_response == actual_response, (
                phase,
                case["name"],
                expected_response,
                actual_response,
            )
        print("Matched generic MCP", phase, "requests", len(selected), flush=True)


def run(binary):
    engine = isolated_engine()
    try:
        owner = seed_observations(engine)
        seed_pairs(engine, owner)
        sources = seed_thresholds(engine, owner)
        cases = requests(owner, sources)
        settings = Settings(_env_file=None, api_tokens=[ApiToken(**token) for token in TOKENS])
        with (
            TestClient(create_app(settings, engine), raise_server_exceptions=False) as python,
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
                    http_validation_compare(python, native)
                    http_compare(python, native, cases, "normal scopes and all operations")
                    configure_phase(engine, "replay")
                    http_compare(python, native, cases, "replay and source-health scope")
            finally:
                server.terminate()
                server.wait(timeout=5)
        configure_phase(engine, "facts")
        with TemporaryDirectory() as directory:
            asyncio.run(mcp_compare(binary, engine, Path(directory), cases))
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
