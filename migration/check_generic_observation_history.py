"""Compare native generic observation history against frozen Python."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from check_generic_entry_history import (
    AT,
    T0,
    T1,
    T2,
    T3,
    TRACKER,
    VERSION1,
    VERSION2,
    audit,
    capture,
    row,
)
from check_generic_entry_history import (
    seed as seed_entries,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import (
    AppState,
    EventMetricMapping,
    Measurement,
    MeasurementRevision,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
    SourcePayload,
)
from garmin_ai.share_policy import TrackerShareConsent

METRIC = UUID(int=91000)
CONTRACT1, CONTRACT2 = UUID(int=91001), UUID(int=91002)


def observation(session, identity, contract=CONTRACT2, **changes):
    values = {
        "id": UUID(int=identity),
        "metric": "user.synthetic_metric",
        "metric_definition_version_id": contract,
        "value": 1.0,
        "unit": "count",
        "observed_at": AT,
        "recorded_at": AT,
        "ingested_at": T0,
        "fetched_at": T0,
        "source_calendar_date": AT.date(),
        "source_ref": UUID(int=identity),
        "timezone": "UTC",
        "quality": "observed",
        "valid": True,
        "sequence": 0,
        "feature_version": "pre-event-v1",
    }
    values.update(changes)
    session.add(MetricObservation(**values))


def measurement(session, source, *, revision=None, **changes):
    values = {
        "ts": AT,
        "metric": "synthetic_metric",
        "source": source,
        "local_date": AT.date(),
        "value": 1.0,
        "unit": "count",
        "metric_definition_version_id": CONTRACT2,
        "source_ref": None,
        "quality": "observed",
        "details": {},
        "ingested_at": T0,
    }
    values.update(changes)
    if revision is None:
        session.add(Measurement(**values))
    else:
        session.add(MeasurementRevision(id=UUID(int=revision), **values))


def seed(engine):
    owner = seed_entries(engine)
    with Session(engine) as session, session.begin():
        system = ensure_system_metric_definitions(session)
        session.add(
            MetricDefinition(
                id=METRIC,
                owner_id=owner,
                namespace="user",
                key="user.synthetic_metric",
                status="retired",
                current_version=2,
            )
        )
        session.flush()
        for version, contract in [(1, CONTRACT1), (2, CONTRACT2)]:
            session.add(
                MetricDefinitionVersion(
                    id=contract,
                    definition_id=METRIC,
                    version=version,
                    value_kind="physical_number",
                    unit="count",
                    dimension="count",
                    aggregation="mean",
                    coverage_policy={"kind": "all_values"},
                    time_semantics="point",
                    labels={},
                    allowed_methods=["mean", "sum"],
                    category_domain=["Я", "😀", "synthetic"] if version == 2 else None,
                    schema_hash="0" * 64,
                )
            )
        session.flush()
        for event_version, metric_version in [(VERSION1, CONTRACT1), (VERSION2, CONTRACT2)]:
            session.add(
                EventMetricMapping(
                    event_definition_version_id=event_version,
                    metric_definition_version_id=metric_version,
                    field_id="synthetic",
                )
            )
        for index, changes in enumerate(
            [
                {},
                {"value": -0.0, "account": "Я😀\x7f\n", "device": 'Synthetic "device"'},
                {"value": 1e-5, "account": "", "device": ""},
                {"value": 1e15},
                {"value": None, "value_text": "Synthetic category"},
                {"value": None, "value_boolean": False},
                {"quality": "unknown"},
                {"valid": False, "invalidated_at": T1},
                {"ingested_at": T2},
                {"source_entry_id": UUID(int=81000), "projection_version": 1},
                {"source_entry_id": UUID(int=81001), "projection_version": 2},
                {"source_entry_id": UUID(int=81002), "projection_version": 3},
                {"observed_at": AT - timedelta(hours=1)},
                {"observed_at": AT + timedelta(hours=3)},
                {"observed_at": None},
                {"feature_version": "source-plugin-v1", "account": "synthetic-provider"},
            ]
        ):
            observation(session, 92000 + index, **changes)
        observation(session, 92100, CONTRACT1, value=11.0)
        for index, snapshot in enumerate([None, []]):
            event_id = UUID(int=99000 + index)
            row(session, event_id, "user.history", VERSION1)
            audit(session, event_id, T1, None, snapshot)
            observation(
                session, 99100 + index, CONTRACT1, source_entry_id=event_id, value=66.0 + index
            )
        observation(session, 92101, system["steps_bucket"].id, value=21.0)
        measurement(session, "legacy", value=2.5)
        measurement(session, "legacy-future", ingested_at=T2)
        measurement(session, "legacy-unknown", quality="unknown")
        measurement(session, "legacy-with-future-revision", value=3.5)
        measurement(session, "legacy-with-future-revision", revision=93000, ingested_at=T2)
        measurement(session, "corrected", revision=93001, value=4.0)
        measurement(session, "corrected", revision=93002, value=5.0, ingested_at=T1)
        measurement(session, "corrected", revision=93003, value=6.0, ingested_at=T2, deleted=True)
        measurement(session, "same-clock", revision=93004, value=7.0, ingested_at=T1)
        measurement(session, "same-clock", revision=93005, value=8.0, ingested_at=T1)
        measurement(session, "same-clock", revision=93006, value=9.0, ingested_at=T1, deleted=True)
        measurement(session, "changed-contract", revision=93007)
        measurement(
            session,
            "changed-contract",
            revision=93008,
            metric_definition_version_id=CONTRACT1,
            ingested_at=T1,
        )
        measurement(session, "changed-quality", revision=93009)
        measurement(session, "changed-quality", revision=93010, quality="unknown", ingested_at=T1)
        measurement(session, "microseconds", ts=AT.replace(microsecond=123000), value=10.0)
        measurement(session, "Unicode Я😀", value=12.0)
    return owner


def arguments(**changes):
    return {
        "operation": "query_observations",
        "metric_key": "user.synthetic_metric",
        "start": (AT - timedelta(hours=1)).isoformat(),
        "end": (AT + timedelta(hours=3)).isoformat(),
        "knowledge_cutoff": T3.isoformat(),
        **changes,
    }


def cases(owner):
    result = [
        {"name": f"cutoff-{index}", "spec": arguments(knowledge_cutoff=cutoff.isoformat())}
        for index, cutoff in enumerate(
            [T0 - timedelta(microseconds=1), T0, T1, T2, T3, T3 + timedelta(days=1)]
        )
    ]
    for index, source in enumerate(
        [
            "event",
            "observation:[null,null]",
            'observation:["",""]',
            'observation:["synthetic-provider",null]',
            "observation:" + json.dumps(["Я😀\x7f\n", 'Synthetic "device"']),
            "measurement:legacy",
            "measurement:corrected",
            "measurement:Unicode Я😀",
            "measurement:absent",
            'observation:["\\u0000",null]',
            'observation:["\\ud800",null]',
            'observation:["\\udfff",null]',
            "measurement:Synthetic\x00source",
        ]
    ):
        result.append({"name": f"source-{index}", "spec": arguments(source=source)})
    result += [
        {"name": "explicit-limit", "spec": arguments(limit=1)},
        {"name": "old-version", "spec": arguments(metric_version=1)},
        {
            "name": "old-version-before-invalid-snapshot",
            "spec": arguments(metric_version=1, knowledge_cutoff=T0.isoformat()),
        },
        {"name": "absent-version", "spec": arguments(metric_version=3)},
        {"name": "large-version", "spec": arguments(metric_version=10**70)},
        {"name": "maximum-version", "spec": arguments(metric_version=2147483647)},
        {"name": "overflow-version", "spec": arguments(metric_version=2147483648)},
        {
            "name": "missing-before-overflow",
            "spec": arguments(metric_key="user.absent", metric_version=2147483648),
        },
        {"name": "absent-metric", "spec": arguments(metric_key="user.absent")},
        {"name": "system-metric", "spec": arguments(metric_key="system.steps_bucket")},
        {"name": "unused-definition", "spec": arguments(definition_key="user.absent")},
        {"name": "limited-owner", "spec": arguments(), "read_health": False},
        {
            "name": "system-limited-owner",
            "spec": arguments(metric_key="system.steps_bucket"),
            "read_health": False,
        },
        {
            "name": "earliest-cutoff",
            "spec": arguments(knowledge_cutoff="0001-01-01T00:00:00+01:00"),
        },
        {"name": "latest-cutoff", "spec": arguments(knowledge_cutoff="9999-12-31T23:59:59-01:00")},
    ]
    return result + [
        {**case, "name": "mcp-" + case["name"], "owner": str(owner)} for case in result
    ]


def compare(binary, engine, fixture, requests, label):
    values = capture(engine, requests)
    for value in values:
        if "row_count" in value:
            assert "expected" in value, value
            assert len(value["expected"]["rows"]) == value["row_count"], value["name"]
    fixture.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    subprocess.run(
        [binary, "--ignored", "--exact", "compares_generic_observation_history"],
        check=True,
        env={**os.environ, "GA_TEST_GENERIC_OBSERVATIONS": str(fixture)},
    )
    print("Matched observation history", label, "cases", len(values), flush=True)


def seed_large(engine, owner):
    with Session(engine) as session, session.begin():
        session.add(
            MetricDefinition(
                id=UUID(int=94000),
                owner_id=owner,
                namespace="user",
                key="user.large_observations",
                status="active",
                current_version=1,
            )
        )
        session.flush()
        for version in [1, 2, 3]:
            session.add(
                MetricDefinitionVersion(
                    id=UUID(int=94000 + version),
                    definition_id=UUID(int=94000),
                    version=version,
                    value_kind="nominal",
                    unit=None,
                    dimension="category",
                    aggregation="counts",
                    coverage_policy={"kind": "all_values"},
                    time_semantics="point",
                    labels={},
                    allowed_methods=["counts"],
                    category_domain=None,
                    schema_hash="0" * 64,
                )
            )
        session.flush()
        for version in [1, 2, 3]:
            session.add(
                EventMetricMapping(
                    event_definition_version_id=VERSION1,
                    metric_definition_version_id=UUID(int=94000 + version),
                    field_id=f"large{version}",
                )
            )
        for index in range(1000):
            observation(
                session,
                95000 + index,
                UUID(int=94001),
                metric="user.large_observations",
                value=None,
                value_text="Я" * 499 + "😀",
                observed_at=AT + timedelta(microseconds=index),
            )
        observation(
            session,
            97000,
            UUID(int=94002),
            metric="user.large_observations",
            value=None,
            value_text="Я" * 4194305,
        )
        for index in range(1001):
            measurement(
                session,
                f"ignored-{index:04}",
                metric_definition_version_id=UUID(int=94003),
            )
        measurement(
            session,
            "selected-after-limit",
            metric_definition_version_id=UUID(int=94003),
            ts=AT + timedelta(microseconds=1),
        )


def run(binary):
    engine = isolated_engine()
    try:
        owner = seed(engine)
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "observations.json"
            for phase in [
                "no_grant",
                "schema",
                "facts",
                "health_only",
                "diary_only",
                "activity_off",
                "replay",
            ]:
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
                    session.execute(text("UPDATE module_configs SET llm_enabled=true"))
                    session.execute(
                        text("DELETE FROM app_state WHERE key LIKE 'tracker-consent:%'")
                    )
                    if phase in {"schema", "facts", "health_only", "diary_only"}:
                        consent = TrackerShareConsent(
                            definition_id=TRACKER,
                            destination_kind="model",
                            destination_instance_id="model:mcp:local",
                            categories={"schema"} if phase == "schema" else {"facts"},
                            granted_at=datetime(2020, 1, 1, tzinfo=UTC),
                        )
                        session.add(
                            AppState(
                                key=f"tracker-consent:{TRACKER}:model:model:mcp:local",
                                value=consent.model_dump(mode="json"),
                            )
                        )
                    if phase == "activity_off":
                        session.execute(
                            text(
                                "UPDATE module_configs SET llm_enabled=false WHERE pack_key='activity'"
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
                                fetched_at=T0,
                                parser_version=0,
                                status="pending",
                                payload={"totalSteps": 0},
                            )
                        )
                compare(binary, engine, fixture, cases(owner), phase)
            with Session(engine) as session, session.begin():
                session.execute(
                    text(
                        "DELETE FROM source_payloads WHERE source='garmin_connect' AND endpoint='daily'"
                    )
                )
                session.execute(text("UPDATE module_configs SET llm_enabled=true"))
                session.execute(
                    text(
                        "UPDATE app_state SET value=CAST(:value AS jsonb) WHERE key='preferences:onboarding'"
                    ),
                    {"value": json.dumps({"model_categories": ["health", "diary"]})},
                )
                consent = TrackerShareConsent(
                    definition_id=TRACKER,
                    destination_kind="model",
                    destination_instance_id="model:mcp:local",
                    categories={"facts"},
                    granted_at=datetime(2020, 1, 1, tzinfo=UTC),
                )
                session.merge(
                    AppState(
                        key=f"tracker-consent:{TRACKER}:model:model:mcp:local",
                        value=consent.model_dump(mode="json"),
                    )
                )
            seed_large(engine, owner)
            large = [
                {
                    "name": "exact-1000",
                    "row_count": 1000,
                    "spec": arguments(metric_key="user.large_observations", limit=1000),
                },
                {
                    "name": "explicit-999",
                    "spec": arguments(metric_key="user.large_observations", limit=999),
                },
                {
                    "name": "large-legacy-value",
                    "row_count": 1,
                    "spec": arguments(metric_key="user.large_observations", metric_version=2),
                },
            ]
            compare(
                binary,
                engine,
                fixture,
                large
                + [{**case, "name": "mcp-" + case["name"], "owner": str(owner)} for case in large],
                "full large values",
            )
            with Session(engine) as session, session.begin():
                observation(
                    session,
                    98000,
                    UUID(int=94001),
                    metric="user.large_observations",
                    value=None,
                    value_text="Synthetic overflow row",
                    account="unique",
                )
            compare(
                binary,
                engine,
                fixture,
                [
                    {
                        "name": "1001-values",
                        "spec": arguments(metric_key="user.large_observations", limit=1000),
                    },
                    {
                        "name": "filter-before-limit",
                        "row_count": 1,
                        "spec": arguments(
                            metric_key="user.large_observations",
                            limit=1,
                            source='observation:["unique",null]',
                        ),
                    },
                    {
                        "name": "measurement-limit",
                        "spec": arguments(
                            metric_key="user.large_observations", metric_version=3, limit=1000
                        ),
                    },
                    {
                        "name": "measurement-filter-before-limit",
                        "row_count": 1,
                        "spec": arguments(
                            metric_key="user.large_observations",
                            metric_version=3,
                            limit=1,
                            source="measurement:selected-after-limit",
                        ),
                    },
                    {
                        "name": "recovered-empty",
                        "row_count": 0,
                        "spec": arguments(
                            metric_key="user.large_observations", limit=1, source="event"
                        ),
                    },
                ],
                "overflow and recovery",
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
