"""Versioned metric contracts and reproducible observation projections."""

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from statistics import median
from types import SimpleNamespace
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, case, func, or_, select, text, update

from garmin_ai.accounts import owner
from garmin_ai.models import (
    AppState,
    Event,
    EventDefinitionVersion,
    EventMetricMapping,
    Measurement,
    MeasurementHistory,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
    SourcePayload,
)

KEY = re.compile(r"^(?:user|system)\.[a-z][a-z0-9_.-]{0,126}$")
SYSTEM_METRIC_REGISTRY_KEY = "registry:metric:catalog_digest"
# Bump when the built-in extras or their contract construction changes.
SYSTEM_METRIC_REGISTRY_REVISION = 1
METHODS = {
    "physical_number": {"latest", "mean", "min", "max", "distribution"},
    "increment": {"sum"},
    "interval_total": {"sum"},
    "cumulative_counter": {"delta", "latest"},
    "ordinal": {"latest", "median", "distribution"},
    "nominal": {"latest", "counts", "mode"},
    "boolean": {"latest", "count_true", "rate"},
}
UNITS = {
    "1": ("dimensionless", 1.0),
    "%": ("ratio", 0.01),
    "bpm": ("frequency", 1.0),
    "rpm": ("frequency", 1.0),
    "count": ("count", 1.0),
    "steps": ("count", 1.0),
    "ms": ("duration", 0.001),
    "s": ("duration", 1.0),
    "minutes": ("duration", 60.0),
    "hours": ("duration", 3600.0),
    "m": ("distance", 1.0),
    "km": ("distance", 1000.0),
    "ml": ("volume", 0.001),
    "L": ("volume", 1.0),
    "mg": ("mass", 0.001),
    "m/s": ("speed", 1.0),
    "km/h": ("speed", 1 / 3.6),
    "s/km": ("pace", 1.0),
    "score": ("ordinal", 1.0),
    "score_1-5": ("ordinal", 1.0),
    "score_1-7": ("ordinal", 1.0),
    "score_1-10": ("ordinal", 1.0),
}


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CoveragePolicy(ContractModel):
    kind: Literal["all_values", "time_weighted", "sparse"]
    minimum_ratio: float | None = Field(default=None, ge=0, le=1)
    max_gap_seconds: int | None = Field(default=None, ge=1, le=86400)

    @model_validator(mode="after")
    def complete(self):
        if self.kind == "time_weighted" and (
            self.minimum_ratio is None or self.max_gap_seconds is None
        ):
            raise ValueError("Time-weighted coverage requires ratio and maximum gap")
        if self.kind != "time_weighted" and (
            self.minimum_ratio is not None or self.max_gap_seconds is not None
        ):
            raise ValueError("Only time-weighted coverage accepts ratio and gap")
        return self


class MetricSpec(ContractModel):
    key: str = Field(pattern=r"^(?:user|system)\.[a-z][a-z0-9_.-]{0,126}$")
    labels: dict[str, str] = Field(min_length=1, max_length=8)
    value_kind: Literal[
        "physical_number",
        "increment",
        "interval_total",
        "cumulative_counter",
        "ordinal",
        "nominal",
        "boolean",
    ]
    unit: str | None = Field(default=None, max_length=32)
    dimension: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    scale_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    scale_version: int | None = Field(default=None, ge=1)
    aggregation: str
    allowed_methods: set[str] = Field(min_length=1, max_length=8)
    coverage: CoveragePolicy
    time_semantics: Literal["point", "interval", "calendar_period"]
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def valid_contract(self):
        if self.time_semantics == "calendar_period":
            raise ValueError("Calendar-period metric windows are not supported")
        if set(self.allowed_methods) - METHODS[self.value_kind]:
            raise ValueError("Metric method is not valid for its value kind")
        if self.aggregation not in self.allowed_methods:
            raise ValueError("Default aggregation must be an allowed method")
        if self.value_kind == "ordinal":
            if self.scale_id is None or self.scale_version is None:
                raise ValueError("Ordinal metrics require a versioned scale")
        elif self.scale_id is not None or self.scale_version is not None:
            raise ValueError("Only ordinal metrics use scale IDs")
        if self.value_kind in {"nominal", "boolean"}:
            if self.unit is not None or self.minimum is not None or self.maximum is not None:
                raise ValueError("Categorical metrics cannot define numeric units or bounds")
        else:
            if self.unit not in UNITS or UNITS[self.unit][0] != self.dimension:
                raise ValueError("Metric unit and dimension do not match")
            if self.minimum is None or self.maximum is None or self.minimum > self.maximum:
                raise ValueError("Numeric metrics require finite ordered bounds")
        if any(not label.strip() or len(label) > 120 for label in self.labels.values()):
            raise ValueError("Metric labels must be nonempty and bounded")
        return self


def metric_hash(spec):
    payload = spec.model_dump(mode="json") if isinstance(spec, MetricSpec) else spec
    if "allowed_methods" in payload:
        payload = {**payload, "allowed_methods": sorted(payload["allowed_methods"])}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def convert_unit(value, source_unit, target_unit):
    if not math.isfinite(value) or source_unit not in UNITS or target_unit not in UNITS:
        raise ValueError("Unknown or non-finite unit value")
    source_dimension, source_factor = UNITS[source_unit]
    target_dimension, target_factor = UNITS[target_unit]
    if {source_dimension, target_dimension} == {"speed", "pace"}:
        if value <= 0:
            raise ValueError("Reciprocal speed and pace conversions require a positive value")
        speed = value * source_factor if source_dimension == "speed" else 1000 / value
        return speed / target_factor if target_dimension == "speed" else 1000 / speed
    if source_dimension != target_dimension:
        raise ValueError("Units have incompatible dimensions")
    return value * source_factor / target_factor


def register_metric_definition(session, spec, *, authorized=False):
    if not authorized:
        raise PermissionError("Metric definition management permission required")
    spec = MetricSpec.model_validate(spec)
    namespace = spec.key.split(".", 1)[0]
    if namespace != "user":
        raise ValueError("User-managed metrics must use the user namespace")
    return _upsert_metric_definition(session, spec, namespace="user")


def _upsert_metric_definition(session, spec, *, namespace):
    definition = session.scalar(select(MetricDefinition).where(MetricDefinition.key == spec.key))
    digest = metric_hash(spec)
    if definition is None:
        definition = MetricDefinition(
            owner_id=owner(session).id if namespace == "user" else None,
            namespace=namespace,
            key=spec.key,
            status="active",
            current_version=1,
        )
        session.add(definition)
        session.flush()
        number = 1
    else:
        if definition.namespace != namespace:
            raise ValueError("Metric namespace cannot be replaced")
        current = current_metric_version(session, definition)
        if current.schema_hash == digest:
            return current
        number = definition.current_version + 1
        definition.current_version = number
    version = MetricDefinitionVersion(
        definition_id=definition.id,
        version=number,
        value_kind=spec.value_kind,
        unit=spec.unit,
        dimension=spec.dimension,
        scale_id=spec.scale_id,
        scale_version=spec.scale_version,
        aggregation=spec.aggregation,
        coverage_policy=spec.coverage.model_dump(mode="json"),
        time_semantics=spec.time_semantics,
        minimum=spec.minimum,
        maximum=spec.maximum,
        labels=spec.labels,
        allowed_methods=sorted(spec.allowed_methods),
        schema_hash=digest,
    )
    session.add(version)
    session.flush()
    return version


def ensure_system_metric_definitions(session, *, backfill=False):
    from garmin_ai.metrics import CATALOG

    session.execute(text("SELECT pg_advisory_xact_lock(72104629)"))
    result = {}
    for key, legacy in CATALOG.items():
        value_kind = "increment" if legacy.kind == "increment" else "physical_number"
        methods = METHODS[value_kind]
        aggregation = legacy.aggregation if legacy.aggregation in methods else "latest"
        coverage = (
            CoveragePolicy(
                kind="time_weighted", minimum_ratio=0.8, max_gap_seconds=legacy.max_gap_seconds
            )
            if legacy.aggregation == "time_weighted_mean"
            else CoveragePolicy(kind="all_values")
        )
        if legacy.aggregation == "time_weighted_mean":
            aggregation = "mean"
        spec = MetricSpec(
            key=f"system.{key}",
            labels={"en": key.replace("_", " ")},
            value_kind=value_kind,
            unit=legacy.unit,
            dimension=UNITS[legacy.unit][0],
            aggregation=aggregation,
            allowed_methods=methods,
            coverage=coverage,
            time_semantics=(
                "interval"
                if value_kind == "increment" or legacy.aggregation == "time_weighted_mean"
                else "point"
            ),
            minimum=legacy.minimum,
            maximum=legacy.maximum if legacy.maximum is not None else 1_000_000_000,
        )
        result[key] = _upsert_metric_definition(session, spec, namespace="system")
    extras = {
        "sleep_score": MetricSpec(
            key="system.sleep_score",
            labels={"en": "sleep score"},
            value_kind="ordinal",
            unit="score",
            dimension="ordinal",
            scale_id="system.garmin_score_0_100",
            scale_version=1,
            aggregation="latest",
            allowed_methods={"latest", "median", "distribution"},
            coverage=CoveragePolicy(kind="sparse"),
            time_semantics="interval",
            minimum=0,
            maximum=100,
        ),
        "training_readiness_score": MetricSpec(
            key="system.training_readiness_score",
            labels={"en": "training readiness score"},
            value_kind="ordinal",
            unit="score",
            dimension="ordinal",
            scale_id="system.garmin_score_0_100",
            scale_version=1,
            aggregation="latest",
            allowed_methods={"latest", "median", "distribution"},
            coverage=CoveragePolicy(kind="sparse"),
            time_semantics="point",
            minimum=0,
            maximum=100,
        ),
        "recovery_time_minutes": MetricSpec(
            key="system.recovery_time_minutes",
            labels={"en": "recovery time"},
            value_kind="physical_number",
            unit="minutes",
            dimension="duration",
            aggregation="latest",
            allowed_methods={"latest", "min", "max"},
            coverage=CoveragePolicy(kind="sparse"),
            time_semantics="point",
            minimum=0,
            maximum=1_000_000,
        ),
    }
    for key, spec in extras.items():
        result[key] = _upsert_metric_definition(session, spec, namespace="system")
    if backfill:
        for key, version in result.items():
            session.execute(
                update(Measurement)
                .where(
                    Measurement.metric == key,
                    Measurement.metric_definition_version_id.is_(None),
                )
                .values(metric_definition_version_id=version.id)
            )
            session.execute(
                update(MetricObservation)
                .where(
                    MetricObservation.metric == key,
                    MetricObservation.metric_definition_version_id.is_(None),
                )
                .values(metric_definition_version_id=version.id)
            )
        marker = session.get(AppState, SYSTEM_METRIC_REGISTRY_KEY)
        if marker is None:
            session.add(
                AppState(
                    key=SYSTEM_METRIC_REGISTRY_KEY,
                    value={"hash": system_metric_registry_digest()},
                )
            )
        else:
            marker.value = {"hash": system_metric_registry_digest()}
    return result


def system_metric_registry_digest():
    from garmin_ai.metrics import CATALOG

    payload = {
        "revision": SYSTEM_METRIC_REGISTRY_REVISION,
        "catalog": {key: asdict(value) for key, value in CATALOG.items()},
        "methods": {key: sorted(value) for key, value in METHODS.items()},
        "units": UNITS,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def ensure_system_metric_definitions_if_needed(session):
    marker = session.get(AppState, SYSTEM_METRIC_REGISTRY_KEY, populate_existing=True)
    if marker is None or marker.value.get("hash") != system_metric_registry_digest():
        ensure_system_metric_definitions(session, backfill=True)


def current_metric_version(session, definition):
    return session.scalar(
        select(MetricDefinitionVersion).where(
            MetricDefinitionVersion.definition_id == definition.id,
            MetricDefinitionVersion.version == definition.current_version,
        )
    )


def bind_event_field(
    session,
    event_definition_version_id: UUID,
    field_id: str,
    metric_definition_version_id: UUID,
    *,
    projection_version=1,
    authorized=False,
):
    if not authorized:
        raise PermissionError("Metric mapping management permission required")
    from garmin_ai.events import lock_writes

    lock_writes(session)
    event_version = session.get(EventDefinitionVersion, event_definition_version_id)
    metric_version = session.get(MetricDefinitionVersion, metric_definition_version_id)
    if event_version is None or metric_version is None:
        raise LookupError("Definition version not found")
    fields = {metadata["id"]: name for name, metadata in event_version.field_metadata.items()}
    if field_id not in fields:
        raise ValueError("Event field identity does not exist")
    metadata = event_version.field_metadata[fields[field_id]]

    def resolved_nodes(node):
        if "$ref" in node:
            return resolved_nodes(
                event_version.schema["$defs"][node["$ref"].removeprefix("#/$defs/")]
            )
        result = []
        for keyword in ("oneOf", "anyOf"):
            for choice in node.get(keyword, []):
                result.extend(resolved_nodes(choice))
        return result or [node]

    property_schema = event_version.schema["properties"][fields[field_id]]
    schema_nodes = resolved_nodes(property_schema)

    def scalar_type(value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        return "unsupported"

    schema_types = set()
    for node in schema_nodes:
        if isinstance(node.get("type"), str):
            schema_types.add(node["type"])
        elif "enum" in node:
            schema_types.update(scalar_type(value) for value in node["enum"])
        elif "const" in node:
            schema_types.add(scalar_type(node["const"]))
    schema_types.discard("null")
    semantic_types = {
        "nominal": {"string"},
        "ordinal": {"integer", "number"},
        "count": {"integer", "number"},
        "quantity": {"integer", "number"},
        "boolean": {"boolean"},
        "text": {"string"},
    }
    if not schema_types or not schema_types <= semantic_types[metadata["semantic"]]:
        raise ValueError("Event field schema type and semantic do not match")
    compatible = {
        "nominal": {"nominal"},
        "ordinal": {"ordinal"},
        "count": {"physical_number", "increment", "interval_total", "cumulative_counter"},
        "quantity": {"physical_number", "increment", "interval_total", "cumulative_counter"},
        "boolean": {"boolean"},
        "text": set(),
    }
    if metric_version.value_kind not in compatible[metadata["semantic"]]:
        raise ValueError("Event field and metric value kinds do not match")
    if metric_version.value_kind in {
        "physical_number",
        "increment",
        "interval_total",
        "cumulative_counter",
        "ordinal",
    }:
        for node in schema_nodes:
            if node.get("type") == "null":
                continue
            minimum = node.get("minimum", node.get("exclusiveMinimum"))
            maximum = node.get("maximum", node.get("exclusiveMaximum"))
            if (
                minimum is None
                or maximum is None
                or minimum < metric_version.minimum
                or maximum > metric_version.maximum
            ):
                raise ValueError("Event field domain range exceeds metric bounds")
    if metric_version.value_kind == "nominal":
        for node in schema_nodes:
            if node.get("type") == "null":
                continue
            values = node.get("enum", [node.get("const")])
            if any(isinstance(value, str) and len(value) > 500 for value in values) or (
                "enum" not in node and "const" not in node and node.get("maxLength", 501) > 500
            ):
                raise ValueError("Event field domain exceeds the metric contract")
    if metric_version.value_kind not in {"nominal", "boolean"} and metadata.get("unit") != (
        metric_version.unit
    ):
        raise ValueError("Event and metric units do not match")

    def numeric_bounds(node):
        if node.get("type") == "null":
            return []
        if "$ref" in node:
            return numeric_bounds(
                event_version.schema["$defs"][node["$ref"].removeprefix("#/$defs/")]
            )
        branches = node.get("oneOf", node.get("anyOf"))
        if branches is not None:
            return [bounds for branch in branches for bounds in numeric_bounds(branch)]
        enum = node.get("enum", [node["const"]] if "const" in node else None)
        if enum is not None:
            values = [
                value
                for value in enum
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            return [(min(values), max(values))] if values else []
        return [
            (
                node.get("minimum", node.get("exclusiveMinimum", -math.inf)),
                node.get("maximum", node.get("exclusiveMaximum", math.inf)),
            )
        ]

    if metric_version.value_kind not in {"nominal", "boolean"}:
        bounds = numeric_bounds(property_schema)
        if not bounds or any(
            lower < metric_version.minimum or upper > metric_version.maximum
            for lower, upper in bounds
        ):
            raise ValueError("Event field range exceeds metric bounds")

    def nominal_domain_is_bounded(node):
        if node.get("type") == "null":
            return True
        if "$ref" in node:
            return nominal_domain_is_bounded(
                event_version.schema["$defs"][node["$ref"].removeprefix("#/$defs/")]
            )
        branches = node.get("oneOf", node.get("anyOf"))
        if branches is not None:
            return all(nominal_domain_is_bounded(branch) for branch in branches)
        enum = node.get("enum", [node["const"]] if "const" in node else None)
        if enum is not None:
            return all(
                value is None or (isinstance(value, str) and 1 <= len(value) <= 500)
                for value in enum
            )
        return node.get("minLength", 0) >= 1 and node.get("maxLength", math.inf) <= 500

    if metric_version.value_kind == "nominal" and not nominal_domain_is_bounded(property_schema):
        raise ValueError("Nominal event field permits empty or oversized values")
    row = EventMetricMapping(
        event_definition_version_id=event_version.id,
        field_id=field_id,
        metric_definition_version_id=metric_version.id,
        projection_version=projection_version,
    )
    session.add(row)
    session.flush()
    for event in session.scalars(
        select(Event)
        .where(Event.definition_version_id == event_version.id, Event.deleted.is_(False))
        .order_by(Event.id)
    ):
        project_event_metrics(session, event, rebuild=True, recorded_at=datetime.now(UTC))
    return row


def _typed_value(version, value):
    if version.value_kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("Metric value must be boolean")
        return None, None, value
    if version.value_kind == "nominal":
        if not isinstance(value, str) or not value or len(value) > 500:
            raise ValueError("Metric value must be a bounded category")
        return None, value, None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Metric value must be a finite number")
    if value < version.minimum or value > version.maximum:
        raise ValueError("Metric value is outside its contract")
    return float(value), None, None


def record_observation(
    session,
    version,
    value,
    *,
    observed_at,
    effective_start=None,
    effective_end=None,
    source_ref,
    timezone="UTC",
    source_entry_id=None,
    field_id=None,
    projection_version=None,
    recorded_at=None,
    uploaded_at=None,
    precision=None,
    coverage=None,
    ingested_at=None,
):
    if observed_at.tzinfo is None or (effective_start and effective_start.tzinfo is None):
        raise ValueError("Observation times must be timezone-aware")
    if effective_end and (
        effective_end.tzinfo is None or effective_end < (effective_start or observed_at)
    ):
        raise ValueError("Observation interval is invalid")
    definition = session.get(MetricDefinition, version.definition_id)
    number, text, boolean = _typed_value(version, value)
    now = ingested_at or datetime.now(UTC)
    row = MetricObservation(
        metric=definition.key,
        value=number,
        value_text=text,
        value_boolean=boolean,
        unit=version.unit or "1",
        metric_definition_version_id=version.id,
        source_entry_id=source_entry_id,
        field_id=field_id,
        projection_version=projection_version,
        observed_at=observed_at,
        effective_start=effective_start or observed_at,
        effective_end=effective_end,
        recorded_at=recorded_at or now,
        uploaded_at=uploaded_at,
        source_calendar_date=observed_at.astimezone(ZoneInfo(timezone)).date(),
        source_ref=source_ref,
        fetched_at=uploaded_at or now,
        ingested_at=now,
        timezone=timezone,
        account=None,
        device=None,
        quality="observed",
        precision=precision,
        coverage=coverage,
        valid=True,
        invalidated_at=None,
        sequence=0,
        feature_version="event-projection-v1" if source_entry_id else "manual-v1",
    )
    session.add(row)
    session.flush()
    return row


def project_event_metrics(session, event, *, rebuild=False, recorded_at=None):
    if event.definition_version_id is None:
        return []
    latest = (
        select(
            EventMetricMapping.field_id,
            func.max(EventMetricMapping.projection_version).label("projection_version"),
        )
        .where(EventMetricMapping.event_definition_version_id == event.definition_version_id)
        .group_by(EventMetricMapping.field_id)
        .subquery()
    )
    mappings = session.scalars(
        select(EventMetricMapping)
        .join(
            latest,
            and_(
                latest.c.field_id == EventMetricMapping.field_id,
                latest.c.projection_version == EventMetricMapping.projection_version,
            ),
        )
        .where(EventMetricMapping.event_definition_version_id == event.definition_version_id)
        .order_by(EventMetricMapping.field_id)
    ).all()
    event_version = session.get(EventDefinitionVersion, event.definition_version_id)
    names = {metadata["id"]: name for name, metadata in event_version.field_metadata.items()}
    projected = []
    revision_time = datetime.now(UTC) if rebuild else None
    if rebuild:
        session.execute(
            update(MetricObservation)
            .where(
                MetricObservation.source_entry_id == event.id,
                MetricObservation.valid.is_(True),
            )
            .values(valid=False, invalidated_at=revision_time)
        )
    for mapping in mappings:
        name = names[mapping.field_id]
        existing = session.scalars(
            select(MetricObservation).where(
                MetricObservation.source_entry_id == event.id,
                MetricObservation.field_id == mapping.field_id,
                MetricObservation.valid.is_(True),
            )
        ).all()
        if name not in event.payload or event.payload[name] is None:
            continue
        if existing and not rebuild:
            projected.extend(existing)
            continue
        if existing:
            session.execute(
                update(MetricObservation)
                .where(MetricObservation.id.in_([row.id for row in existing]))
                .values(valid=False, invalidated_at=datetime.now(UTC))
            )
        generation = (
            session.scalar(
                select(func.max(MetricObservation.projection_version)).where(
                    MetricObservation.source_entry_id == event.id,
                    MetricObservation.field_id == mapping.field_id,
                )
            )
            or 0
        ) + 1
        metric_version = session.get(MetricDefinitionVersion, mapping.metric_definition_version_id)
        projected.append(
            record_observation(
                session,
                metric_version,
                event.payload[name],
                observed_at=event.start,
                effective_start=event.start,
                effective_end=event.end,
                source_ref=event.id,
                timezone=event.timezone,
                source_entry_id=event.id,
                field_id=mapping.field_id,
                projection_version=generation,
                recorded_at=recorded_at or event.recorded_at,
                ingested_at=revision_time,
            )
        )
    return projected


def _row_value(row):
    if row.value is not None:
        return row.value
    if row.value_text is not None:
        return row.value_text
    return row.value_boolean


def aggregate_metric(session, key, start, end, *, method=None, version=None, knowledge_cutoff=None):
    from garmin_ai.events import event_query_allowed

    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("Metric window must be a bounded aware interval")
    if end - start > timedelta(days=366):
        raise ValueError("Metric window exceeds 366 days")
    explicit_cutoff = knowledge_cutoff is not None
    knowledge_cutoff = knowledge_cutoff or datetime.now(UTC)
    if knowledge_cutoff.tzinfo is None:
        raise ValueError("Knowledge cutoff must be timezone-aware")
    definition = session.scalar(select(MetricDefinition).where(MetricDefinition.key == key))
    if definition is None:
        raise LookupError("Metric definition not found")
    number = version or definition.current_version
    contract = session.scalar(
        select(MetricDefinitionVersion).where(
            MetricDefinitionVersion.definition_id == definition.id,
            MetricDefinitionVersion.version == number,
        )
    )
    if contract.time_semantics == "calendar_period":
        raise ValueError("Calendar-period metric windows are not supported")
    method = method or contract.aggregation
    if method not in contract.allowed_methods:
        raise ValueError("Aggregation is not allowed by this metric version")
    policy = contract.coverage_policy
    predecessor_start = (
        start - timedelta(seconds=policy["max_gap_seconds"])
        if policy["kind"] == "time_weighted"
        else start
    )
    if contract.time_semantics == "interval":
        if contract.value_kind in {"increment", "interval_total"}:
            # A total cannot be apportioned to an arbitrary partial window.
            time_filter = or_(
                and_(
                    MetricObservation.effective_end.is_not(None),
                    MetricObservation.effective_start >= start,
                    MetricObservation.effective_end <= end,
                ),
                and_(
                    MetricObservation.effective_end.is_(None),
                    MetricObservation.observed_at >= start,
                    MetricObservation.observed_at < end,
                ),
            )
        else:
            time_filter = or_(
                and_(
                    MetricObservation.effective_end.is_not(None),
                    MetricObservation.effective_start < end,
                    MetricObservation.effective_end > start,
                ),
                and_(
                    MetricObservation.effective_end.is_(None),
                    MetricObservation.effective_start < MetricObservation.observed_at,
                    MetricObservation.effective_start < end,
                    MetricObservation.observed_at > start,
                ),
                and_(
                    MetricObservation.effective_end.is_(None),
                    MetricObservation.observed_at >= predecessor_start,
                    MetricObservation.observed_at < end,
                ),
            )
    else:
        time_filter = and_(
            MetricObservation.observed_at >= start,
            MetricObservation.observed_at < end,
        )
    snapshot_rank = (
        func.row_number()
        .over(
            partition_by=(
                MetricObservation.feature_version,
                case(
                    (MetricObservation.feature_version == "pre-event-v1", None),
                    else_=MetricObservation.id,
                ),
                MetricObservation.observed_at,
                MetricObservation.source_calendar_date,
                MetricObservation.sequence,
                MetricObservation.account,
                MetricObservation.device,
            ),
            order_by=(
                MetricObservation.ingested_at.desc(),
                MetricObservation.fetched_at.desc(),
                MetricObservation.id.desc(),
            ),
        )
        .label("snapshot_rank")
    )
    ranked = (
        select(MetricObservation.id.label("observation_id"), snapshot_rank)
        .where(
            MetricObservation.metric_definition_version_id == contract.id,
            or_(
                MetricObservation.valid.is_(True),
                MetricObservation.invalidated_at > knowledge_cutoff,
            ),
            MetricObservation.quality == "observed",
            or_(
                MetricObservation.source_entry_id.is_(None),
                MetricObservation.source_entry_id.in_(
                    select(Event.id).where(event_query_allowed())
                ),
            ),
            time_filter,
            MetricObservation.observed_at <= knowledge_cutoff,
            MetricObservation.ingested_at <= knowledge_cutoff,
        )
        .subquery()
    )
    rows = session.scalars(
        select(MetricObservation)
        .join(ranked, ranked.c.observation_id == MetricObservation.id)
        .where(ranked.c.snapshot_rank == 1)
        .order_by(
            MetricObservation.observed_at,
            MetricObservation.recorded_at,
            MetricObservation.ingested_at,
            MetricObservation.sequence,
            MetricObservation.id,
        )
        .limit(10001)
    ).all()
    measurement_start = predecessor_start if contract.time_semantics == "interval" else start
    measurement_known = SourcePayload.fetched_at <= knowledge_cutoff
    if not explicit_cutoff:
        # Older manually imported measurements may have no retained source payload.
        # They can inform a current answer, but cannot establish historical knowledge.
        measurement_known = or_(measurement_known, SourcePayload.id.is_(None))
    measurements = session.execute(
        select(Measurement, SourcePayload.fetched_at)
        .outerjoin(SourcePayload, Measurement.source_ref == SourcePayload.id)
        .where(
            Measurement.metric_definition_version_id == contract.id,
            Measurement.quality == "observed",
            Measurement.ts >= measurement_start,
            Measurement.ts < end,
            Measurement.ts <= knowledge_cutoff,
            measurement_known,
        )
        .order_by(Measurement.ts, Measurement.metric, Measurement.source)
        .limit(10001)
    ).all()
    history_by_key = {}
    if explicit_cutoff:
        for historical in session.scalars(
            select(MeasurementHistory)
            .where(
                MeasurementHistory.metric_definition_version_id == contract.id,
                MeasurementHistory.quality == "observed",
                MeasurementHistory.ts >= measurement_start,
                MeasurementHistory.ts < end,
                MeasurementHistory.ts <= knowledge_cutoff,
                MeasurementHistory.known_at <= knowledge_cutoff,
                MeasurementHistory.superseded_at > knowledge_cutoff,
            )
            .order_by(MeasurementHistory.known_at.desc(), MeasurementHistory.id.desc())
            .limit(10001)
        ):
            history_by_key.setdefault(
                (historical.ts, historical.metric, historical.source), historical
            )
    rows.extend(
        SimpleNamespace(
            id=f"measurement-history:{row.id}",
            value=row.value,
            value_text=None,
            value_boolean=None,
            observed_at=row.ts,
            effective_start=row.ts,
            effective_end=None,
            source_ref=row.source_ref,
            recorded_at=row.known_at,
            ingested_at=row.known_at,
            sequence=0,
        )
        for row in history_by_key.values()
    )
    rows.extend(
        SimpleNamespace(
            id=f"measurement:{row.metric}:{row.source}:{row.ts.isoformat()}",
            value=row.value,
            value_text=None,
            value_boolean=None,
            observed_at=row.ts,
            effective_start=row.ts,
            effective_end=None,
            source_ref=row.source_ref,
            recorded_at=fetched_at or row.ts,
            ingested_at=fetched_at or knowledge_cutoff,
            sequence=0,
        )
        for row, fetched_at in measurements
        if (row.ts, row.metric, row.source) not in history_by_key
    )
    rows.sort(
        key=lambda row: (
            row.observed_at,
            row.recorded_at or row.ingested_at,
            row.ingested_at,
            row.sequence or 0,
            str(row.id),
        )
    )
    if contract.value_kind in {"increment", "interval_total"}:
        rows = [
            row
            for row in rows
            if row.effective_end is None
            or ((row.effective_start or row.observed_at) >= start and row.effective_end <= end)
        ]
    if len(rows) > 10000:
        raise ValueError("Metric query exceeds 10000 observations")
    interval_ends = {}
    if policy["kind"] == "time_weighted":
        for index, row in enumerate(rows):
            following = rows[index + 1].observed_at if index + 1 < len(rows) else None
            maximum = row.observed_at + timedelta(seconds=policy["max_gap_seconds"])
            interval_ends[row.id] = (
                row.effective_end
                if row.effective_end is not None
                else row.observed_at
                if row.effective_start is not None and row.effective_start < row.observed_at
                else min(following, maximum)
                if following is not None
                else maximum
            )
        rows = [
            row
            for row in rows
            if interval_ends[row.id] > start and (row.effective_start or row.observed_at) < end
        ]
    delta_predecessor = None
    if method == "delta" and rows:
        prior = session.scalar(
            select(MetricObservation)
            .where(
                MetricObservation.metric_definition_version_id == contract.id,
                or_(
                    MetricObservation.valid.is_(True),
                    MetricObservation.invalidated_at > knowledge_cutoff,
                ),
                MetricObservation.quality == "observed",
                or_(
                    MetricObservation.source_entry_id.is_(None),
                    MetricObservation.source_entry_id.in_(
                        select(Event.id).where(event_query_allowed())
                    ),
                ),
                MetricObservation.observed_at < start,
                MetricObservation.ingested_at <= knowledge_cutoff,
            )
            .order_by(
                MetricObservation.observed_at.desc(),
                MetricObservation.ingested_at.desc(),
                MetricObservation.id.desc(),
            )
            .limit(1)
        )
        candidates = []
        if prior is not None:
            candidates.append((prior.observed_at, prior.ingested_at, _row_value(prior)))
        prior_measurement = session.execute(
            select(Measurement, SourcePayload.fetched_at)
            .outerjoin(SourcePayload, Measurement.source_ref == SourcePayload.id)
            .where(
                Measurement.metric_definition_version_id == contract.id,
                Measurement.quality == "observed",
                Measurement.ts < start,
                Measurement.ts <= knowledge_cutoff,
                measurement_known,
            )
            .order_by(Measurement.ts.desc(), SourcePayload.fetched_at.desc())
            .limit(1)
        ).first()
        if prior_measurement is not None:
            measurement, fetched_at = prior_measurement
            candidates.append((measurement.ts, fetched_at or measurement.ts, measurement.value))
        if explicit_cutoff:
            prior_history = session.scalar(
                select(MeasurementHistory)
                .where(
                    MeasurementHistory.metric_definition_version_id == contract.id,
                    MeasurementHistory.quality == "observed",
                    MeasurementHistory.ts < start,
                    MeasurementHistory.ts <= knowledge_cutoff,
                    MeasurementHistory.known_at <= knowledge_cutoff,
                    MeasurementHistory.superseded_at > knowledge_cutoff,
                )
                .order_by(
                    MeasurementHistory.ts.desc(),
                    MeasurementHistory.known_at.desc(),
                    MeasurementHistory.id.desc(),
                )
                .limit(1)
            )
            if prior_history is not None:
                candidates.append((prior_history.ts, prior_history.known_at, prior_history.value))
        if candidates:
            delta_predecessor = max(candidates, key=lambda item: (item[0], item[1]))[2]
    values = [_row_value(row) for row in rows]
    result = None
    if values:
        if method == "sum":
            result = sum(values)
        elif method == "mean":
            result = sum(values) / len(values)
        elif method == "min":
            result = min(values)
        elif method == "max":
            result = max(values)
        elif method == "latest":
            result = values[-1]
        elif method == "median":
            result = median(values)
        elif method in {"distribution", "counts"}:
            result = dict(Counter(str(value) for value in values))
        elif method == "mode":
            result = Counter(values).most_common(1)[0][0]
        elif method == "count_true":
            result = sum(value is True for value in values)
        elif method == "rate":
            result = sum(value is True for value in values) / len(values)
        elif method == "delta":
            delta_values = ([delta_predecessor] if delta_predecessor is not None else []) + values
            if len(delta_values) >= 2:
                result = sum(
                    current - previous if current >= previous else current
                    for previous, current in zip(delta_values, delta_values[1:], strict=False)
                )
    coverage_ratio = None
    if policy["kind"] == "time_weighted":

        def interval_end(row):
            return interval_ends[row.id]

        intervals = sorted(
            (
                max(row.effective_start or row.observed_at, start),
                min(interval_end(row), end),
            )
            for row in rows
            if interval_end(row) > start and (row.effective_start or row.observed_at) < end
        )
        merged = []
        for left, right in intervals:
            if left >= right:
                continue
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        seconds = sum((right - left).total_seconds() for left, right in merged)
        coverage_ratio = min(1, seconds / (end - start).total_seconds())
        boundaries = [start, *(point for interval in merged for point in interval), end]
        gaps = [
            (boundaries[index + 1] - boundaries[index]).total_seconds()
            for index in range(0, len(boundaries) - 1, 2)
        ]
        if method in {"mean", "rate"} and rows:
            weighted = [
                (
                    max(
                        0,
                        (
                            min(interval_end(row), end)
                            - max(row.effective_start or row.observed_at, start)
                        ).total_seconds(),
                    ),
                    _row_value(row),
                )
                for row in rows
            ]
            denominator = sum(seconds for seconds, _ in weighted)
            result = (
                sum(
                    seconds * (value is True if method == "rate" else value)
                    for seconds, value in weighted
                )
                / denominator
                if denominator
                else None
            )
        if coverage_ratio < policy["minimum_ratio"] or max(gaps) > policy["max_gap_seconds"]:
            result = None
    return {
        "metric": key,
        "metric_version": number,
        "value_kind": contract.value_kind,
        "method": method,
        "value": result,
        "unit": contract.unit,
        "scale_id": contract.scale_id,
        "scale_version": contract.scale_version,
        "coverage_ratio": coverage_ratio,
        "observations": len(rows),
        "source_refs": [str(row.source_ref) for row in rows[:100]],
        "knowledge_cutoff": knowledge_cutoff.isoformat(),
        "latest_known_at": max((row.ingested_at.isoformat() for row in rows), default=None),
    }
