"""Synthetic paired windows for generic period and completeness reads."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from capture_metric_aggregates import CUTOFF, END, START
from check_generic_entry_history import VERSION1
from sqlalchemy.orm import Session

from garmin_ai.models import (
    EventMetricMapping,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
)

METRICS = {
    "paired_numeric": (
        "physical_number",
        ["mean", "min", "max", "latest", "distribution", "median", "sum"],
        [1.0, 3.0],
        [4.0, 8.0],
    ),
    "paired_zero": ("physical_number", ["latest"], [0.0], [-0.0]),
    "paired_bool": ("boolean", ["latest", "count_true", "rate"], [False, False], [False, True]),
    "paired_nominal": (
        "nominal",
        ["mode", "counts", "latest"],
        ["Synthetic A", "Synthetic B"],
        ["Synthetic B", "Synthetic B"],
    ),
    "paired_counters": ("cumulative_counter", ["delta", "latest"], [10.0, 15.0], [3.0, 8.0]),
    "paired_sources": ("physical_number", ["mean"], [1.0], [5.0]),
    "paired_empty": ("physical_number", ["mean"], [], []),
    "paired_weighted": ("physical_number", ["mean"], [1.0, 3.0], [2.0, 10.0]),
    "paired_total": ("interval_total", ["sum"], [13.0, 17.0], [3.0, 4.0]),
}


def seed(engine, owner):
    with Session(engine) as session, session.begin():
        for metric_index, (key, (kind, methods, first, second)) in enumerate(METRICS.items()):
            definition = UUID(int=4000000 + metric_index * 2)
            version = UUID(int=4000001 + metric_index * 2)
            session.add(
                MetricDefinition(
                    id=definition,
                    owner_id=owner,
                    namespace="user",
                    key="user." + key,
                    current_version=1,
                    status="active",
                )
            )
            session.flush()
            weighted = key == "paired_weighted"
            session.add(
                MetricDefinitionVersion(
                    id=version,
                    definition_id=definition,
                    version=1,
                    value_kind=kind,
                    unit=None if kind in {"nominal", "boolean"} else "count",
                    dimension="category"
                    if kind == "nominal"
                    else "boolean"
                    if kind == "boolean"
                    else "count",
                    aggregation=methods[0],
                    allowed_methods=methods,
                    coverage_policy={
                        "kind": "time_weighted",
                        "minimum_ratio": 0.0,
                        "max_gap_seconds": 3600,
                    }
                    if weighted
                    else {"kind": "all_values"},
                    time_semantics="interval" if weighted or kind == "interval_total" else "point",
                    labels={},
                    schema_hash="0" * 64,
                )
            )
            session.flush()
            session.add(
                EventMetricMapping(
                    event_definition_version_id=VERSION1,
                    metric_definition_version_id=version,
                    field_id=key,
                )
            )
            windows = [first, second]
            if kind == "cumulative_counter":
                windows = [[7.0], first, second]
            for window, values in enumerate(windows):
                for sample, value in enumerate(values):
                    actual_window = window - 1 if kind == "cumulative_counter" else window
                    at = START + timedelta(
                        hours=actual_window, minutes=sample * 30 if weighted else sample * 10 + 10
                    )
                    identity = UUID(int=4100000 + metric_index * 100 + window * 10 + sample)
                    session.add(
                        MetricObservation(
                            id=identity,
                            metric="user." + key,
                            metric_definition_version_id=version,
                            value=value
                            if isinstance(value, (float, int)) and not isinstance(value, bool)
                            else None,
                            value_boolean=value if isinstance(value, bool) else None,
                            value_text=value if isinstance(value, str) else None,
                            unit="1" if kind in {"nominal", "boolean"} else "count",
                            observed_at=at,
                            effective_start=at if weighted or kind == "interval_total" else None,
                            effective_end=at + timedelta(minutes=30 if weighted else 1)
                            if weighted or kind == "interval_total"
                            else None,
                            recorded_at=START - timedelta(hours=2),
                            ingested_at=START - timedelta(hours=2),
                            source_calendar_date=at.date(),
                            source_ref=identity,
                            fetched_at=START - timedelta(hours=2),
                            timezone="UTC",
                            quality="observed",
                            valid=True,
                            sequence=0,
                            feature_version="synthetic-paired-v1",
                            account=f"source-{actual_window}" if key == "paired_sources" else None,
                        )
                    )

        for identity, key, at, value, finish in [
            (4200000, "paired_numeric", datetime(1, 1, 1, 0, 30, tzinfo=UTC), 42.0, None),
            (4200001, "paired_numeric", datetime(9999, 12, 31, 23, 30, tzinfo=UTC), 84.0, None),
            (
                4200002,
                "paired_weighted",
                datetime(1, 1, 1, tzinfo=UTC),
                7.0,
                datetime(1, 1, 1, 1, 30, tzinfo=UTC),
            ),
        ]:
            version = UUID(int=4000001 + list(METRICS).index(key) * 2)
            session.add(
                MetricObservation(
                    id=UUID(int=identity),
                    metric="user." + key,
                    metric_definition_version_id=version,
                    value=value,
                    unit="count",
                    observed_at=at,
                    effective_start=at if finish else None,
                    effective_end=finish,
                    recorded_at=START - timedelta(hours=2),
                    ingested_at=START - timedelta(hours=2),
                    source_calendar_date=at.date(),
                    source_ref=UUID(int=identity),
                    fetched_at=START - timedelta(hours=2),
                    timezone="UTC",
                    quality="observed",
                    valid=True,
                    sequence=0,
                    feature_version="synthetic-calendar-v1",
                )
            )


def arguments(key, operation, **changes):
    result = {
        "operation": operation,
        "metric_key": "user." + key,
        "start": START.isoformat(),
        "end": END.isoformat(),
        "knowledge_cutoff": CUTOFF.isoformat(),
    }
    if operation == "compare_periods":
        result.update(
            comparison_start=END.isoformat(), comparison_end=(END + timedelta(hours=1)).isoformat()
        )
    return {**result, **changes}


def cases(owner):
    result = []
    for key, (_kind, methods, _first, _second) in METRICS.items():
        for method in methods:
            result.append(
                {
                    "name": f"complete-{key}-{method}",
                    "spec": arguments(key, "query_completeness", method=method),
                }
            )
            for index, (start, end) in enumerate(
                [
                    (END, END + timedelta(hours=1)),
                    (START, END),
                    (END + timedelta(hours=1), END + timedelta(hours=2)),
                ]
            ):
                result.append(
                    {
                        "name": f"compare-{key}-{method}-{index}",
                        "spec": arguments(
                            key,
                            "compare_periods",
                            method=method,
                            comparison_start=start.isoformat(),
                            comparison_end=end.isoformat(),
                        ),
                    }
                )
    for key in ["paired_numeric", "paired_weighted"]:
        for index, (start, end) in enumerate(
            [
                ("0001-01-01T12:00:00+14:00", "0001-01-01T13:00:00+14:00"),
                ("9999-12-31T23:00:00-14:00", "9999-12-31T23:59:00-14:00"),
                ("0001-01-01T00:00:00+14:00", "0001-01-01T01:00:00+14:00"),
            ]
        ):
            result.append(
                {
                    "name": f"calendar-{key}-{index}",
                    "spec": arguments(
                        key,
                        "compare_periods",
                        source="observation:[null,null]",
                        comparison_start=start,
                        comparison_end=end,
                    ),
                }
            )
    result.append(
        {
            "name": "explicit-paired-source",
            "spec": arguments(
                "paired_sources", "compare_periods", source='observation:["source-0",null]'
            ),
        }
    )
    result.extend(
        [
            {
                "name": "known-data-across-lower-calendar",
                "spec": arguments(
                    "paired_numeric",
                    "compare_periods",
                    comparison_start="0001-01-01T12:00:00+14:00",
                    comparison_end="0001-01-02T00:00:00+14:00",
                ),
            },
            {
                "name": "known-data-across-upper-calendar",
                "spec": arguments(
                    "paired_numeric",
                    "compare_periods",
                    comparison_start="9999-12-31T08:00:00-14:00",
                    comparison_end="9999-12-31T12:00:00-14:00",
                    knowledge_cutoff="9999-12-31T23:59:59.999999Z",
                ),
            },
            {
                "name": "weighted-data-across-lower-calendar",
                "spec": arguments(
                    "paired_weighted",
                    "compare_periods",
                    comparison_start="0001-01-01T13:30:00+14:00",
                    comparison_end="0001-01-01T16:00:00+14:00",
                ),
            },
        ]
    )
    return result + [
        {**case, "name": "mcp-" + case["name"], "owner": str(owner)} for case in result
    ]
