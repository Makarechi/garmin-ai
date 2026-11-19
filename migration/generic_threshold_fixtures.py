"""Synthetic measurement histories for bounded threshold interval reports."""

import random
from datetime import UTC, datetime, timedelta
from uuid import UUID

from check_generic_entry_history import VERSION1
from check_generic_observation_history import observation
from sqlalchemy.orm import Session

from garmin_ai.models import (
    EventMetricMapping,
    Measurement,
    MeasurementRevision,
    MetricDefinition,
    MetricDefinitionVersion,
)

START = datetime(2026, 11, 1, 4, tzinfo=UTC)
END = START + timedelta(hours=5)
CUTOFF = START + timedelta(days=2)
CONTRACT = UUID(int=6000001)
OTHER = UUID(int=6000003)


def seed(engine, owner):
    with Session(engine) as session, session.begin():
        for index, (key, kind, policy) in enumerate(
            [
                ("primary", "physical_number", "time_weighted"),
                ("ordinary", "physical_number", "all_values"),
                ("ordinal", "ordinal", "time_weighted"),
            ]
        ):
            definition, version = UUID(int=6000000 + index * 2), UUID(int=6000001 + index * 2)
            session.add(
                MetricDefinition(
                    id=definition,
                    owner_id=owner,
                    namespace="user",
                    key="user.threshold_" + key,
                    current_version=1,
                    status="active",
                )
            )
            session.flush()
            session.add(
                MetricDefinitionVersion(
                    id=version,
                    definition_id=definition,
                    version=1,
                    value_kind=kind,
                    unit="count",
                    dimension="count",
                    minimum=0,
                    maximum=100,
                    aggregation="mean" if kind == "physical_number" else "median",
                    allowed_methods=["mean"] if kind == "physical_number" else ["median"],
                    coverage_policy={
                        "kind": policy,
                        **(
                            {"minimum_ratio": 0.0, "max_gap_seconds": 3600}
                            if policy == "time_weighted"
                            else {}
                        ),
                    },
                    time_semantics="point",
                    labels={},
                    schema_hash="0" * 64,
                )
            )
            session.flush()
            session.add(
                EventMetricMapping(
                    event_definition_version_id=VERSION1,
                    metric_definition_version_id=version,
                    field_id="threshold_" + key,
                )
            )
        session.flush()
        randomizer = random.Random(1729)
        histories = {
            "basic": [
                (minute, value)
                for minute, value in [
                    (-10, 10),
                    (0, 51),
                    (10, 80),
                    (20, 20),
                    (30, 90),
                    (100, 80),
                    (110, 80),
                    (120, 10),
                    (180, 80),
                    (240, 30),
                    (300, 80),
                    (310, 10),
                ]
            ],
            "empty": [],
            "single": [(10, 80)],
            "zeros": [(0, -0.0), (10, 0.0), (20, 0.0)],
            "wrong-unit": [(0, 80), (10, 80), (20, 80), (30, 80)],
            "invalid-range": [(0, 80), (10, -1), (20, 101), (30, 80), (40, 80)],
            "nan": [(0, 80), (10, float("nan")), (20, 80), (30, 80)],
            "unknown-nan": [(0, 80), (10, float("nan")), (20, 80), (30, 80)],
            "revision-nan": [(0, 80), (10, float("nan")), (20, 80), (30, 80)],
            "positive-infinity": [(0, 80), (10, float("inf")), (20, 80), (30, 80)],
            "negative-infinity": [(0, 80), (10, -float("inf")), (20, 80), (30, 80)],
            "revision": [(0, 80), (10, 80), (20, 80), (30, 80)],
            "deleted": [(0, 80), (10, 80), (20, 80), (30, 80)],
            "changed-contract": [(0, 80), (10, 80), (20, 80), (30, 80)],
            "future-revision": [(0, 80), (10, 80), (20, 80), (30, 80)],
            "same-instant": [(0, 80), (10, 80), (20, 80)],
            "fractional": [(index / 60_000_000, 51 + index % 2) for index in range(25)],
            "Unicode Я😀": [(0, 80), (10, 80)],
        }
        for index in range(30):
            minutes = sorted(randomizer.sample(range(-59, 359), 30))
            histories[f"random-{index}"] = [
                (minute, randomizer.choice([0.0, -0.0, 10.0, 51.0, 80.0, 100.0, -1.0, 101.0]))
                for minute in minutes
            ]
        for source_index, (source, points) in enumerate(histories.items()):
            for point, (minute, value) in enumerate(points):
                at = START + timedelta(minutes=minute)
                values = dict(
                    ts=at,
                    metric="threshold_primary",
                    source=source,
                    local_date=at.date(),
                    value=value,
                    unit="score" if source == "wrong-unit" and point == 1 else "count",
                    metric_definition_version_id=CONTRACT,
                    source_ref=None,
                    quality="unknown" if source == "unknown-nan" and point == 1 else "observed",
                    details={"synthetic": "Я😀"},
                    ingested_at=START - timedelta(days=1),
                )
                session.add(Measurement(**values))
                if source in {
                    "revision",
                    "revision-nan",
                    "deleted",
                    "changed-contract",
                    "future-revision",
                }:
                    identity = 6100000 + source_index * 100 + point * 2
                    session.add(MeasurementRevision(id=UUID(int=identity), **values))
                    if point == 1:
                        updated = {**values, "ingested_at": START + timedelta(days=1)}
                        if source in {"revision", "revision-nan"}:
                            updated["quality"] = "invalid"
                        if source == "deleted":
                            updated["deleted"] = True
                        if source == "changed-contract":
                            updated["metric_definition_version_id"] = OTHER
                        if source == "future-revision":
                            updated["ingested_at"] = CUTOFF + timedelta(days=1)
                        session.add(MeasurementRevision(id=UUID(int=identity + 1), **updated))
                if source == "same-instant":
                    session.add(
                        Measurement(
                            **{**values, "metric": "threshold_primary:alternate", "value": 20}
                        )
                    )
        for index in range(1001):
            at = START + timedelta(seconds=index)
            for source in ["limit", "full-limit"] if index < 1000 else ["limit"]:
                session.add(
                    Measurement(
                        ts=at,
                        metric="threshold_primary",
                        source=source,
                        local_date=at.date(),
                        value=80 if index % 2 == 0 else 10,
                        unit="count",
                        metric_definition_version_id=CONTRACT,
                        source_ref=None,
                        quality="observed",
                        details={},
                        ingested_at=START - timedelta(days=1),
                    )
                )
        for index, options in enumerate(
            [
                {},
                {"account": "Я😀", "device": "synthetic"},
                {"feature_version": "source-plugin-v1", "account": "threshold-provider"},
            ]
        ):
            observation(
                session,
                6200000 + index,
                CONTRACT,
                observed_at=START,
                recorded_at=START,
                ingested_at=START - timedelta(days=1),
                **options,
            )
        observation(
            session,
            6200100,
            CONTRACT,
            observed_at=START + timedelta(days=10),
            ingested_at=START - timedelta(days=1),
            account="only-observation",
        )
    return list(histories)


def request(**changes):
    return {
        "operation": "threshold_segments",
        "metric_key": "user.threshold_primary",
        "start": START.isoformat(),
        "end": END.isoformat(),
        "knowledge_cutoff": CUTOFF.isoformat(),
        "threshold": 51,
        "threshold_operator": "gte",
        **changes,
    }


def cases(owner, sources):
    results = []
    for source in sources:
        for operator in ["gt", "gte", "lt", "lte"]:
            for operation, timezone in [
                ("threshold_segments", None),
                ("threshold_by_local_hour", "America/New_York"),
                ("threshold_by_local_hour", "Asia/Kathmandu"),
            ]:
                results.append(
                    {
                        "name": f"{source}-{operator}-{operation}-{timezone}",
                        "spec": request(
                            source="measurement:" + source,
                            threshold_operator=operator,
                            operation=operation,
                            **({"timezone": timezone} if timezone else {}),
                        ),
                    }
                )
    extra = [
        {
            "start": (START + timedelta(days=10)).isoformat(),
            "end": (START + timedelta(days=10, hours=1)).isoformat(),
        },
        {},
        {"limit": 1},
        {"source": "measurement:limit", "limit": 1000},
        {"source": "measurement:full-limit", "limit": 1000},
        {"source": "measurement:basic", "limit": 1},
        {"source": "measurement:basic", "limit": 12},
        {"source": "event"},
        {"source": "observation:[null,null]"},
        {"source": "observation:" + '["\\ud800",null]'},
        {"source": "measurement:" + chr(0)},
        {"metric_key": "user.absent"},
        {"metric_version": 2},
        {"metric_version": 2147483648},
        {"metric_key": "user.threshold_ordinary"},
        {"metric_key": "user.threshold_ordinal"},
        {"source": "measurement:basic", "definition_key": "user.absent"},
        {
            "source": "measurement:revision",
            "knowledge_cutoff": (START + timedelta(hours=1)).isoformat(),
        },
        {
            "source": "measurement:basic",
            "knowledge_cutoff": (START + timedelta(minutes=15)).isoformat(),
        },
        {
            "source": "measurement:basic",
            "start": (START + timedelta(minutes=5)).isoformat(),
            "end": (START + timedelta(minutes=25)).isoformat(),
        },
        {
            "source": "measurement:basic",
            "start": "0001-01-01T00:00:00Z",
            "end": "0001-01-01T01:00:00Z",
        },
        {
            "source": "measurement:basic",
            "start": "9999-12-31T22:00:00Z",
            "end": "9999-12-31T23:59:59Z",
        },
        {
            "source": "measurement:basic",
            "start": (START - timedelta(days=15)).isoformat(),
            "end": (START + timedelta(days=16)).isoformat(),
        },
    ]
    for index, changes in enumerate(extra):
        for operation in ["threshold_segments", "threshold_by_local_hour"]:
            results.append(
                {
                    "name": f"edge-{index}-{operation}",
                    "spec": request(
                        **changes,
                        operation=operation,
                        **(
                            {"timezone": "America/New_York"}
                            if operation == "threshold_by_local_hour"
                            else {}
                        ),
                    ),
                }
            )
    for source in ["unknown-nan", "revision-nan"]:
        spec = request(source="measurement:" + source, operation="aggregate_metric")
        del spec["threshold"]
        del spec["threshold_operator"]
        results.append({"name": "aggregate-invalid-nonfinite-" + source, "spec": spec})
    results += [
        {"name": "limited-owner", "read_health": False, "spec": request(source="measurement:basic")}
    ]
    return results + [
        {**case, "name": "mcp-" + case["name"], "owner": str(owner)} for case in results
    ]
