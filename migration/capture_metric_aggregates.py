"""Capture aggregate arithmetic from the frozen application on synthetic stored metrics."""

import argparse
import inspect
import json
import math
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.metric_definitions import aggregate_metric
from garmin_ai.models import MetricDefinition, MetricDefinitionVersion, MetricObservation, Person

START = datetime(2026, 11, 1, 8, tzinfo=UTC)
END = START + timedelta(hours=1)
CUTOFF = END + timedelta(days=1)


def micros(at):
    elapsed = at.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds


def requests():
    methods = {
        "physical_number": ["mean", "min", "max", "latest", "distribution", "median", "sum"],
        "increment": ["sum"],
        "interval_total": ["sum"],
        "cumulative_counter": ["delta", "latest"],
        "ordinal": ["median", "latest", "distribution"],
        "nominal": ["counts", "mode", "latest"],
        "boolean": ["count_true", "rate", "latest"],
    }
    cases = []
    for kind, allowed in methods.items():
        arrays = (
            [[], [False], [True, False, True]]
            if kind == "boolean"
            else [[], ["Я"], ["Я", "😀", "😀", "Я"]]
            if kind == "nominal"
            else [[], [1.0], [1.0, 2.0, 3.0, 4.0], [1e16, 1.0, -1e16], [-0.0, 0.0]]
        )
        for method in allowed:
            for values in arrays:
                cases.append(
                    {
                        "value_kind": kind,
                        "method": method,
                        "values": values,
                        "policy": {"kind": "all_values"},
                    }
                )
    for method in ["sum", "mean", "min", "median"]:
        cases.append(
            {
                "value_kind": "physical_number",
                "method": method,
                "values": [1.0, "Synthetic incompatible value"],
                "policy": {"kind": "all_values"},
            }
        )
    cases.append(
        {
            "value_kind": "nominal",
            "method": "mode",
            "values": [True, 1.0, False, 0.0, False],
            "policy": {"kind": "all_values"},
        }
    )
    cases.append(
        {
            "value_kind": "physical_number",
            "method": "sum",
            "values": [1e308, 1e308],
            "policy": {"kind": "all_values"},
        }
    )
    for intervals in [
        [(0, 1800), (1800, 3600)],
        [(0, 2700), (1800, 3600)],
        [(0, 900), (1800, 2700)],
    ]:
        for kind, method, values in [
            ("physical_number", "mean", [0.1, 0.2]),
            ("boolean", "rate", [False, True]),
        ]:
            cases.append(
                {
                    "value_kind": kind,
                    "method": method,
                    "values": values,
                    "intervals": intervals,
                    "policy": {
                        "kind": "time_weighted",
                        "minimum_ratio": 0.0,
                        "max_gap_seconds": 86400,
                    },
                }
            )
    for caps in [[None, None], [0.25, None], [0.5, None]]:
        cases.append(
            {
                "value_kind": "physical_number",
                "method": "mean",
                "values": [0.1, 0.2],
                "intervals": [(0, 900), (1800, 2700)],
                "coverage": caps,
                "policy": {"kind": "time_weighted", "minimum_ratio": 0.5, "max_gap_seconds": 900},
            }
        )
    cases.append(
        {
            "value_kind": "physical_number",
            "method": "latest",
            "values": [1.0],
            "intervals": [(1800, 900)],
            "coverage": [-0.0],
            "policy": {"kind": "time_weighted", "minimum_ratio": 0.0, "max_gap_seconds": 86400},
        }
    )
    rng = random.Random(99146)
    for _ in range(600):
        kind = rng.choice(list(methods))
        method = rng.choice(methods[kind])
        count = rng.randrange(31)
        if kind == "nominal":
            values = [
                rng.choice(["Synthetic A", "Я", "😀", "Synthetic long " * 30]) for _ in range(count)
            ]
        elif kind == "boolean":
            values = [rng.choice([True, False]) for _ in range(count)]
        else:
            values = [
                rng.choice([rng.randrange(-1000, 1001) / 7, 1e-5, 1e15, -0.0, 1e-100])
                for _ in range(count)
            ]
        policy = {"kind": rng.choice(["all_values", "sparse", "time_weighted"])}
        if policy["kind"] == "time_weighted":
            policy.update(
                minimum_ratio=rng.choice([0.0, 0.25, 0.9, 1.0]),
                max_gap_seconds=rng.choice([120, 600, 3600, 86400]),
            )
        cases.append(
            {
                "value_kind": kind,
                "method": method,
                "values": values,
                "policy": policy,
                "seed": rng.randrange(2**31),
                "predecessor": rng.choice([None, 0.0, 100.0])
                if kind == "cumulative_counter"
                else None,
            }
        )
    return cases


def add_observation(session, case_index, index, contract, value, at, **changes):
    values = {
        "id": UUID(int=2000000 + case_index * 100 + index),
        "metric": f"user.synthetic_{case_index}",
        "metric_definition_version_id": contract,
        "value": None,
        "value_text": None,
        "value_boolean": None,
        "unit": "count",
        "observed_at": at,
        "effective_start": None,
        "effective_end": None,
        "recorded_at": START - timedelta(hours=1),
        "ingested_at": START - timedelta(hours=1),
        "source_calendar_date": at.date(),
        "source_ref": UUID(int=2000000 + case_index * 100 + index),
        "fetched_at": START - timedelta(hours=1),
        "timezone": "UTC",
        "quality": "observed",
        "valid": True,
        "sequence": 0,
        "feature_version": "synthetic-aggregate-v1",
        "coverage": None,
    }
    if isinstance(value, bool):
        values["value_boolean"] = value
    elif isinstance(value, str):
        values["value_text"] = value
    else:
        values["value"] = value
    values.update(changes)
    session.add(MetricObservation(**values))


def capture(session, index, request, owner):
    definition = UUID(int=1000000 + index * 2)
    contract = UUID(int=1000001 + index * 2)
    key = f"user.synthetic_{index}"
    session.add(
        MetricDefinition(
            id=definition,
            owner_id=owner,
            namespace="user",
            key=key,
            current_version=1,
            status="active",
        )
    )
    session.flush()
    session.add(
        MetricDefinitionVersion(
            id=contract,
            definition_id=definition,
            version=1,
            value_kind=request["value_kind"],
            unit="count",
            dimension="count",
            aggregation=request["method"],
            coverage_policy=request["policy"],
            time_semantics="interval" if request["value_kind"] == "interval_total" else "point",
            labels={},
            allowed_methods=[request["method"]],
            schema_hash="0" * 64,
        )
    )
    session.flush()
    rng = random.Random(request.get("seed", 0))
    for sample_index, value in enumerate(request["values"]):
        at = START + timedelta(minutes=sample_index * 2, microseconds=rng.randrange(1000))
        changes = {}
        if request["value_kind"] == "interval_total":
            changes.update(effective_start=at, effective_end=at + timedelta(seconds=30))
        elif request["policy"]["kind"] == "time_weighted":
            if rng.randrange(4) == 0:
                changes["effective_start"] = at - timedelta(seconds=rng.randrange(60))
            if rng.randrange(4) == 0:
                changes["effective_end"] = at + timedelta(seconds=rng.randrange(1, 600))
            if rng.randrange(8) == 0:
                changes["coverage"] = rng.choice([0.0, 0.25, 0.75, 1.0])
        if "intervals" in request:
            left, right = request["intervals"][sample_index]
            changes.update(
                effective_start=START + timedelta(seconds=left),
                effective_end=START + timedelta(seconds=right),
            )
        if "coverage" in request:
            changes["coverage"] = request["coverage"][sample_index]
        add_observation(session, index, sample_index, contract, value, at, **changes)
    if request.get("predecessor") is not None:
        add_observation(
            session, index, 99, contract, request["predecessor"], START - timedelta(minutes=1)
        )
    session.flush()
    lines, first_line = inspect.getsourcelines(aggregate_metric)
    capture_line = first_line + next(
        offset
        for offset, line in enumerate(lines)
        if line.strip() == "values = [_row_value(row) for row in rows]"
    )
    captured = {}

    def trace(frame, event, _argument):
        if (
            frame.f_code is aggregate_metric.__code__
            and event == "line"
            and frame.f_lineno == capture_line
        ):
            local = frame.f_locals
            captured.update(
                value_kind=request["value_kind"],
                method=request["method"],
                policy=request["policy"],
                start=micros(START),
                end=micros(END),
                predecessor=local["delta_predecessor"],
                samples=[
                    {
                        "value": row.value
                        if row.value is not None
                        else row.value_text
                        if row.value_text is not None
                        else row.value_boolean,
                        "interval_start": micros(row.effective_start or row.observed_at),
                        "interval_end": micros(local["interval_ends"][row.id])
                        if request["policy"]["kind"] == "time_weighted"
                        else None,
                        "coverage": row.coverage,
                    }
                    for row in local["rows"]
                ],
            )
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        try:
            result = aggregate_metric(
                session, key, START, END, method=request["method"], knowledge_cutoff=CUTOFF
            )
            captured["expected"] = {key: result[key] for key in ["value", "coverage_ratio"]}
            try:
                json.dumps(captured["expected"], allow_nan=False)
            except ValueError:
                captured.pop("expected")
                captured.update(error="ValueError", error_phase="json")
        except Exception as error:
            captured["error"] = type(error).__name__
    finally:
        sys.settrace(previous)
    assert "samples" in captured, (index, request, captured)
    return captured


def run(output):
    engine = isolated_engine()
    try:
        cases = []
        with Session(engine) as session, session.begin():
            owner = session.scalar(select(Person.id))
            for index, request in enumerate(requests()):
                cases.append(capture(session, index, request, owner))
        assert all(
            math.isfinite(case["expected"]["coverage_ratio"])
            for case in cases
            if case.get("expected", {}).get("coverage_ratio") is not None
        )
        output.write_text(
            "[\n"
            + ",\n".join(
                json.dumps(case, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                for case in cases
            )
            + "\n]\n",
            encoding="utf-8",
        )
        print(
            "Captured frozen metric aggregate cases",
            len(cases),
            "errors",
            sum("error" in case for case in cases),
            flush=True,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
