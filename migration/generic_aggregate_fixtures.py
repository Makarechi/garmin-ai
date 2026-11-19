"""Synthetic aggregate history, interval and resource-boundary fixtures."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from capture_metric_aggregates import CUTOFF, END, START
from check_generic_entry_history import T0, TRACKER, VERSION1
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.models import (
    AppState,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    EventMetricMapping,
    Measurement,
    MeasurementHistory,
    MeasurementRevision,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
    SourcePayload,
)
from garmin_ai.share_policy import TrackerShareConsent

KEYS = [
    ("counter_measurement", "cumulative_counter", "delta", "point"),
    ("history_measurement", "physical_number", "mean", "point"),
    ("pre_event", "physical_number", "mean", "point"),
    ("interval_total", "interval_total", "sum", "interval"),
    ("interval_increment", "increment", "sum", "interval"),
    ("interval_numeric", "physical_number", "mean", "interval"),
    ("discarded_increment", "increment", "sum", "point"),
    ("weighted_legacy", "physical_number", "mean", "interval"),
    ("bound_metric", "increment", "sum", "point"),
    ("bound_measurement", "physical_number", "mean", "point"),
    ("calendar_rejected", "physical_number", "mean", "calendar_period"),
    ("nominal_bound", "nominal", "counts", "point"),
    ("invalid_event_permission", "physical_number", "mean", "point"),
]
CONTRACTS = {name: UUID(int=3100001 + index * 2) for index, (name, *_) in enumerate(KEYS)}


def observation(session, identity, name, value, at=START, **changes):
    values = {
        "id": UUID(int=identity),
        "metric": "user." + name,
        "metric_definition_version_id": CONTRACTS[name],
        "value": value,
        "unit": "count",
        "observed_at": at,
        "recorded_at": START - timedelta(hours=1),
        "ingested_at": START - timedelta(hours=1),
        "source_calendar_date": at.date(),
        "source_ref": UUID(int=identity),
        "fetched_at": START - timedelta(hours=1),
        "timezone": "UTC",
        "quality": "observed",
        "valid": True,
        "sequence": 0,
        "feature_version": "synthetic-edge-v1",
    }
    values.update(changes)
    session.add(MetricObservation(**values))


def measurement(session, name, source, value, at=START, **changes):
    values = {
        "ts": at,
        "metric": name,
        "source": source,
        "local_date": at.date(),
        "value": value,
        "unit": "count",
        "metric_definition_version_id": CONTRACTS[name],
        "source_ref": None,
        "quality": "observed",
        "details": {},
        "ingested_at": START - timedelta(hours=1),
    }
    values.update(changes)
    session.add(Measurement(**values))


def seed(engine, owner):
    with Session(engine) as session, session.begin():
        for index, (name, kind, method, semantics) in enumerate(KEYS):
            definition = UUID(int=3100000 + index * 2)
            session.add(
                MetricDefinition(
                    id=definition,
                    owner_id=owner,
                    namespace="user",
                    key="user." + name,
                    current_version=1,
                    status="active",
                )
            )
            session.flush()
            policy = (
                {"kind": "time_weighted", "minimum_ratio": 0.0, "max_gap_seconds": 3600}
                if name == "weighted_legacy"
                else {"kind": "all_values"}
            )
            session.add(
                MetricDefinitionVersion(
                    id=CONTRACTS[name],
                    definition_id=definition,
                    version=1,
                    value_kind=kind,
                    unit=None if kind == "nominal" else "count",
                    dimension="category" if kind == "nominal" else "count",
                    aggregation=method,
                    coverage_policy=policy,
                    time_semantics=semantics,
                    labels={},
                    allowed_methods=[method],
                    schema_hash="0" * 64,
                )
            )
            session.flush()
            session.add(
                EventMetricMapping(
                    event_definition_version_id=VERSION1,
                    metric_definition_version_id=CONTRACTS[name],
                    field_id=f"edge_{index}",
                )
            )
        legacy_definition = UUID(int=3900000)
        legacy_version = UUID(int=3900001)
        session.add(
            EventDefinition(
                id=legacy_definition,
                owner_id=owner,
                namespace="user",
                key="user.invalid_operations",
                status="active",
                current_version=1,
            )
        )
        session.flush()
        session.add(
            EventDefinitionVersion(
                id=legacy_version,
                definition_id=legacy_definition,
                version=1,
                schema={},
                schema_hash="0" * 64,
                topology="point",
                field_metadata={},
                labels={},
                privacy="private",
                allowed_operations={"query": True},
            )
        )
        session.flush()
        session.add(
            EventMetricMapping(
                event_definition_version_id=legacy_version,
                metric_definition_version_id=CONTRACTS["invalid_event_permission"],
                field_id="legacy_permission",
            )
        )
        session.add(
            Event(
                id=UUID(int=3910000),
                kind="user.invalid_operations",
                definition_version_id=legacy_version,
                start=START,
                timezone="UTC",
                source="manual",
                topology="point",
                payload={"description": "Synthetic legacy operations"},
                recorded_at=START - timedelta(hours=1),
                ingested_at=START - timedelta(hours=1),
                updated_at=START - timedelta(hours=1),
            )
        )
        session.flush()
        observation(
            session,
            3910001,
            "invalid_event_permission",
            99.0,
            source_entry_id=UUID(int=3910000),
            projection_version=1,
        )
        measurement(session, "counter_measurement", "primary", 10.0, START - timedelta(minutes=1))
        measurement(session, "counter_measurement", "outside", 17.0, START - timedelta(seconds=30))
        measurement(session, "counter_measurement", "primary", 20.0, START + timedelta(minutes=1))
        measurement(
            session,
            "history_measurement",
            "retained",
            9.0,
            START + timedelta(minutes=2),
            ingested_at=START + timedelta(hours=5),
        )
        for index, (known, value) in enumerate(
            [(START - timedelta(hours=2), 5.0), (START - timedelta(hours=1), 6.0)]
        ):
            session.add(
                MeasurementHistory(
                    id=UUID(int=3300000 + index),
                    ts=START + timedelta(minutes=2),
                    metric="history_measurement",
                    source="retained",
                    value=value,
                    metric_definition_version_id=CONTRACTS["history_measurement"],
                    source_ref=UUID(int=3310000 + index),
                    quality="observed",
                    known_at=known,
                    superseded_at=START + timedelta(hours=3),
                )
            )
        payload = UUID(int=3320000)
        session.add(
            SourcePayload(
                id=payload,
                source="synthetic",
                endpoint="synthetic_measurement",
                source_key="activation",
                payload_hash="d" * 64,
                archive_key="sha256:" + "d" * 64,
                fetched_at=START - timedelta(hours=3),
                parser_version=9,
                status="normalized",
                payload={},
            )
        )
        session.flush()
        measurement(
            session,
            "history_measurement",
            "activation",
            7.0,
            START + timedelta(minutes=3),
            source_ref=payload,
            ingested_at=START + timedelta(hours=5),
        )
        measurement(
            session,
            "history_measurement",
            "suppressed",
            9.0,
            START + timedelta(minutes=4),
            ingested_at=START + timedelta(hours=5),
        )
        session.add(
            MeasurementHistory(
                id=UUID(int=3330000),
                ts=START + timedelta(minutes=4),
                metric="history_measurement",
                source="suppressed",
                value=8.0,
                metric_definition_version_id=CONTRACTS["history_measurement"],
                source_ref=UUID(int=3330001),
                quality="observed",
                known_at=START - timedelta(hours=1),
                superseded_at=START + timedelta(hours=3),
            )
        )
        session.add(
            MeasurementRevision(
                id=UUID(int=3330002),
                ts=START + timedelta(minutes=4),
                metric="history_measurement",
                source="suppressed",
                value=9.0,
                unit="count",
                local_date=START.date(),
                metric_definition_version_id=CONTRACTS["history_measurement"],
                source_ref=UUID(int=3330003),
                quality="observed",
                details={},
                ingested_at=START - timedelta(hours=1),
                deleted=True,
            )
        )
        for index, (value, ingested, fetched) in enumerate(
            [
                (1.0, START - timedelta(hours=2), START - timedelta(hours=2)),
                (2.0, START + timedelta(minutes=1), START + timedelta(minutes=1)),
                (3.0, START + timedelta(minutes=1), START + timedelta(minutes=2)),
                (4.0, START + timedelta(minutes=1), START + timedelta(minutes=2)),
            ]
        ):
            observation(
                session,
                3400000 + index,
                "pre_event",
                value,
                ingested_at=ingested,
                fetched_at=fetched,
                feature_version="pre-event-v1",
            )
        for identity, name, value in [
            (3410000, "interval_total", 13.0),
            (3410001, "interval_increment", 7.0),
            (3410002, "interval_numeric", 8.0),
        ]:
            observation(
                session,
                identity,
                name,
                value,
                START - timedelta(hours=1),
                effective_start=START + timedelta(minutes=1),
                effective_end=START + timedelta(minutes=2),
            )
        observation(
            session,
            3410003,
            "discarded_increment",
            3.0,
            effective_start=START - timedelta(minutes=1),
        )
        measurement(session, "weighted_legacy", "weighted", 1.0, START)
        measurement(session, "weighted_legacy", "weighted", 3.0, START + timedelta(minutes=5))
        observation(
            session,
            3410004,
            "weighted_legacy",
            5.0,
            datetime(9999, 12, 31, 23, 30, tzinfo=UTC),
            effective_start=datetime(9999, 12, 31, 23, 30, tzinfo=UTC),
            effective_end=datetime(9999, 12, 31, 23, 45, tzinfo=UTC),
        )
        session.flush()
        session.execute(
            text("""
            INSERT INTO metric_observations(id,metric,metric_definition_version_id,value,unit,observed_at,
                recorded_at,ingested_at,source_calendar_date,source_ref,fetched_at,timezone,quality,valid,sequence,feature_version)
            SELECT ('00000000-0000-0000-0000-'||lpad(to_hex(3500000+index),12,'0'))::uuid,
                'user.bound_metric',:contract,1,'count',:at,:known,:known,CAST(:at AS date),
                ('00000000-0000-0000-0000-'||lpad(to_hex(3500000+index),12,'0'))::uuid,
                :known,'UTC','observed',true,0,'synthetic-boundary-v1'
            FROM generate_series(0,9999) AS parts(index)
        """),
            {
                "contract": CONTRACTS["bound_metric"],
                "at": START,
                "known": START - timedelta(hours=1),
            },
        )

        session.execute(
            text("""
            INSERT INTO measurements(ts,metric,source,local_date,value,unit,metric_definition_version_id,
                quality,details,ingested_at)
            SELECT :at+index*interval '1 microsecond','bound_measurement','bounded',CAST(:at AS date),1,
                'count',:contract,'observed','{}'::jsonb,:known
            FROM generate_series(0,10000) AS parts(index)
        """),
            {
                "contract": CONTRACTS["bound_measurement"],
                "at": START,
                "known": START - timedelta(hours=1),
            },
        )

        session.execute(
            text("""
            INSERT INTO metric_observations(id,metric,metric_definition_version_id,value_text,unit,observed_at,
                recorded_at,ingested_at,source_calendar_date,source_ref,fetched_at,timezone,quality,valid,sequence,feature_version)
            SELECT ('00000000-0000-0000-0000-'||lpad(to_hex(3700000+index),12,'0'))::uuid,
                'user.nominal_bound',:contract,repeat('😀',494)||lpad(index::text,6,'0'),'',:at,:known,:known,CAST(:at AS date),
                ('00000000-0000-0000-0000-'||lpad(to_hex(3700000+index),12,'0'))::uuid,
                :known,'UTC','observed',true,0,'synthetic-boundary-v1'
            FROM generate_series(0,9999) AS parts(index)
        """),
            {
                "contract": CONTRACTS["nominal_bound"],
                "at": START,
                "known": START - timedelta(hours=1),
            },
        )


def request(name, **changes):
    return {
        "operation": "aggregate_metric",
        "metric_key": "user." + name,
        "start": START.isoformat(),
        "end": END.isoformat(),
        "knowledge_cutoff": CUTOFF.isoformat(),
        **changes,
    }


def cases(owner):
    result = [
        {
            "name": "counter-inferred-predecessor",
            "expected_value": 3.0,
            "spec": request("counter_measurement"),
        },
        {
            "name": "counter-explicit-predecessor",
            "expected_value": 10.0,
            "spec": request("counter_measurement", source="measurement:primary"),
        },
        {
            "name": "counter-empty-source",
            "spec": request("counter_measurement", source="measurement:outside"),
        },
        {
            "name": "history-retained",
            "expected_value": 6.0,
            "spec": request(
                "history_measurement",
                source="measurement:retained",
                knowledge_cutoff=(START + timedelta(hours=1)).isoformat(),
            ),
        },
        {
            "name": "history-activation",
            "expected_value": 7.0,
            "spec": request(
                "history_measurement",
                source="measurement:activation",
                knowledge_cutoff=(START + timedelta(hours=1)).isoformat(),
            ),
        },
        {
            "name": "history-suppressed",
            "expected_value": None,
            "spec": request(
                "history_measurement",
                source="measurement:suppressed",
                knowledge_cutoff=(START + timedelta(hours=1)).isoformat(),
            ),
        },
        {
            "name": "pre-event-old",
            "expected_value": 1.0,
            "spec": request("pre_event", knowledge_cutoff=START.isoformat()),
        },
        {"name": "pre-event-ranked", "expected_value": 4.0, "spec": request("pre_event")},
        {"name": "interval-total", "expected_value": 13.0, "spec": request("interval_total")},
        {
            "name": "interval-increment",
            "expected_value": 7.0,
            "spec": request("interval_increment"),
        },
        {"name": "interval-numeric", "expected_value": 8.0, "spec": request("interval_numeric")},
        {
            "name": "invalid-recorded-operations",
            "expected_value": None,
            "spec": request("invalid_event_permission"),
        },
        {
            "name": "invalid-operations-observations",
            "row_count": 0,
            "spec": request("invalid_event_permission", operation="query_observations"),
        },
        {"name": "10000-full-categories", "spec": request("nominal_bound")},
        {
            "name": "discarded-increment",
            "expected_value": None,
            "spec": request("discarded_increment"),
        },
        {
            "name": "weighted-legacy-id",
            "spec": request("weighted_legacy", source="measurement:weighted"),
        },
        {
            "name": "minimum-calendar-overflow",
            "spec": request(
                "weighted_legacy", start="0001-01-01T00:00:00Z", end="0001-01-01T01:00:00Z"
            ),
        },
        {
            "name": "overflow-before-encoding",
            "spec": request(
                "weighted_legacy",
                start="0001-01-01T00:00:00Z",
                end="0001-01-01T01:00:00Z",
                source="observation:" + json.dumps([chr(0xD800), None]),
            ),
        },
        {
            "name": "maximum-calendar-overflow",
            "spec": request(
                "weighted_legacy",
                start="9999-12-31T23:00:00Z",
                end="9999-12-31T23:59:59.999999Z",
                knowledge_cutoff="9999-12-31T23:59:59.999999Z",
            ),
        },
        {
            "name": "exact-10000",
            "expected_value": 10000.0,
            "spec": request("bound_metric", limit=1),
        },
        {"name": "calendar-period", "spec": request("calendar_rejected")},
        {"name": "calendar-before-method", "spec": request("calendar_rejected", method="latest")},
        {
            "name": "calendar-before-encoding",
            "spec": request(
                "calendar_rejected", source="observation:" + json.dumps([chr(0xD800), None])
            ),
        },
        {"name": "version-before-calendar", "spec": request("calendar_rejected", metric_version=2)},
        {"name": "10001-measurement-snapshot", "spec": request("bound_measurement")},
        {
            "name": "10000-measurement-snapshot",
            "expected_value": 1.0,
            "spec": request(
                "bound_measurement", end=(START + timedelta(microseconds=10000)).isoformat()
            ),
        },
    ]
    return result + [
        {**case, "name": "mcp-" + case["name"], "owner": str(owner)} for case in result
    ]


def configure_phase(engine, phase):
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
        session.execute(text("DELETE FROM app_state WHERE key LIKE 'tracker-consent:%'"))
        session.execute(
            text(
                "DELETE FROM source_payloads WHERE source='garmin_connect' AND endpoint='daily' AND payload_hash=:hash"
            ),
            {"hash": "b" * 64},
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
                text("UPDATE module_configs SET llm_enabled=false WHERE pack_key='activity'")
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


def add_overflow(engine):
    with Session(engine) as session, session.begin():
        observation(session, 3600000, "bound_metric", 2.0, START + timedelta(microseconds=1))
        observation(
            session,
            3600001,
            "bound_metric",
            2.0,
            START + timedelta(microseconds=2),
            account="unique",
        )
