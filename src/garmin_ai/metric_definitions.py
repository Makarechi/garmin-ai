"""Versioned metric contracts and reproducible observation projections."""

import hashlib
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from statistics import median
from types import SimpleNamespace
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, case, func, or_, select, update

from garmin_ai.accounts import owner
from garmin_ai.models import (
    EventDefinitionVersion,
    EventMetricMapping,
    Measurement,
    MeasurementRevision,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
)

KEY = re.compile(r"^(?:user|system)\.[a-z][a-z0-9_.-]{0,126}$")
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
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "m/s": ("speed", 1.0),
    "km/h": ("speed", 1 / 3.6),
    "s/km": ("pace", 1.0),
    "score": ("ordinal", 1.0),
    "score_1-5": ("ordinal", 1.0),
    "score_1-7": ("ordinal", 1.0),
    "score_1-10": ("ordinal", 1.0),
}
SCORE_UNIT = re.compile(r"^score_-?\d+--?\d+$")


def unit_dimension(unit):
    """Return the registered dimension, including bounded tracker scales."""

    if unit in UNITS:
        return UNITS[unit][0]
    if isinstance(unit, str) and SCORE_UNIT.fullmatch(unit):
        return "ordinal"
    return None


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
    category_domain: list[str] | None = Field(default=None, min_length=1, max_length=50)
    coverage: CoveragePolicy
    time_semantics: Literal["point", "interval", "calendar_period"]
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def valid_contract(self):
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
            if unit_dimension(self.unit) != self.dimension:
                raise ValueError("Metric unit and dimension do not match")
            if self.minimum is None or self.maximum is None or self.minimum > self.maximum:
                raise ValueError("Numeric metrics require finite ordered bounds")
        if self.value_kind != "nominal" and self.category_domain is not None:
            raise ValueError("Only nominal metrics define a category domain")
        if self.category_domain is not None and (
            len(set(self.category_domain)) != len(self.category_domain)
            or any(not value.strip() or len(value) > 200 for value in self.category_domain)
        ):
            raise ValueError("Metric category domain must be unique and bounded")
        if any(not label.strip() or len(label) > 120 for label in self.labels.values()):
            raise ValueError("Metric labels must be nonempty and bounded")
        return self


def metric_hash(spec):
    payload = spec.model_dump(mode="json") if isinstance(spec, MetricSpec) else spec
    if payload.get("category_domain") is None:
        payload = {key: value for key, value in payload.items() if key != "category_domain"}
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
            raise ValueError(
                "Zero speed or pace has no reciprocal unit conversion; negative values are invalid"
            )
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
        category_domain=spec.category_domain,
        schema_hash=digest,
    )
    session.add(version)
    session.flush()
    return version


def ensure_system_metric_definitions(session, *, backfill=False):
    from garmin_ai.metrics import CATALOG

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
                update(MeasurementRevision)
                .where(
                    MeasurementRevision.metric == key,
                    MeasurementRevision.metric_definition_version_id.is_(None),
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
    return result


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
    schema_types = {node["type"] for node in schema_nodes if isinstance(node.get("type"), str)}
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
                raise ValueError("Event field domain exceeds the metric contract")
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
    row = EventMetricMapping(
        event_definition_version_id=event_version.id,
        field_id=field_id,
        metric_definition_version_id=metric_version.id,
        projection_version=projection_version,
    )
    session.add(row)
    session.flush()
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


def project_event_metrics(session, event, *, rebuild=False):
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
                recorded_at=event.recorded_at,
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


def measurement_rows_as_of(session, contract_id, start, end, knowledge_cutoff, *, limit=10001):
    """Return the last revision known at the cutoff for each measurement identity."""

    rank = (
        func.row_number()
        .over(
            partition_by=(
                MeasurementRevision.ts,
                MeasurementRevision.metric,
                MeasurementRevision.source,
            ),
            order_by=(
                MeasurementRevision.ingested_at.desc(),
                MeasurementRevision.deleted.asc(),
                MeasurementRevision.id.desc(),
            ),
        )
        .label("snapshot_rank")
    )
    ranked = (
        select(MeasurementRevision.id.label("revision_id"), rank)
        .where(
            MeasurementRevision.metric_definition_version_id == contract_id,
            MeasurementRevision.quality == "observed",
            MeasurementRevision.ts >= start,
            MeasurementRevision.ts < end,
            MeasurementRevision.ingested_at <= knowledge_cutoff,
        )
        .subquery()
    )
    revisions_query = (
        select(MeasurementRevision)
        .join(ranked, ranked.c.revision_id == MeasurementRevision.id)
        .where(
            ranked.c.snapshot_rank == 1,
            MeasurementRevision.deleted.is_(False),
        )
        .order_by(
            MeasurementRevision.ts,
            MeasurementRevision.metric,
            MeasurementRevision.source,
        )
    )
    if limit is not None:
        revisions_query = revisions_query.limit(limit)
    revisions = session.scalars(revisions_query).all()
    has_revision = (
        select(MeasurementRevision.id)
        .where(
            MeasurementRevision.ts == Measurement.ts,
            MeasurementRevision.metric == Measurement.metric,
            MeasurementRevision.source == Measurement.source,
        )
        .exists()
    )
    legacy_query = (
        select(Measurement)
        .where(
            Measurement.metric_definition_version_id == contract_id,
            Measurement.quality == "observed",
            Measurement.ts >= start,
            Measurement.ts < end,
            Measurement.ingested_at <= knowledge_cutoff,
            ~has_revision,
        )
        .order_by(Measurement.ts, Measurement.metric, Measurement.source)
    )
    if limit is not None:
        legacy_query = legacy_query.limit(limit)
    legacy_rows = session.scalars(legacy_query).all()
    rows = sorted(
        [*revisions, *legacy_rows],
        key=lambda row: (row.ts, row.metric, row.source),
    )
    return rows[:limit] if limit is not None else rows


def aggregate_metric(session, key, start, end, *, method=None, version=None, knowledge_cutoff=None):
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("Metric window must be a bounded aware interval")
    if (end - start).days > 366:
        raise ValueError("Metric window exceeds 366 days")
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
    method = method or contract.aggregation
    if method not in contract.allowed_methods:
        raise ValueError("Aggregation is not allowed by this metric version")
    policy = contract.coverage_policy
    predecessor_start = (
        start - timedelta(seconds=policy["max_gap_seconds"])
        if policy["kind"] == "time_weighted"
        else start
    )
    time_filter = (
        or_(
            and_(
                MetricObservation.effective_end.is_not(None),
                MetricObservation.effective_start < end,
                MetricObservation.effective_end > start,
            ),
            and_(
                MetricObservation.effective_end.is_(None),
                MetricObservation.observed_at >= predecessor_start,
                MetricObservation.observed_at < end,
            ),
        )
        if contract.time_semantics == "interval"
        else and_(
            MetricObservation.observed_at >= start,
            MetricObservation.observed_at < end,
        )
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
            time_filter,
            MetricObservation.ingested_at <= knowledge_cutoff,
        )
        .subquery()
    )
    rows = session.scalars(
        select(MetricObservation)
        .join(ranked, ranked.c.observation_id == MetricObservation.id)
        .where(ranked.c.snapshot_rank == 1)
        .order_by(MetricObservation.observed_at, MetricObservation.id)
        .limit(10001)
    ).all()
    measurement_start = predecessor_start if contract.time_semantics == "interval" else start
    measurements = measurement_rows_as_of(
        session,
        contract.id,
        measurement_start,
        end,
        knowledge_cutoff,
        limit=10001,
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
            ingested_at=row.ingested_at,
        )
        for row in measurements
    )
    rows.sort(key=lambda row: (row.observed_at, str(row.id)))
    if len(rows) > 10000:
        raise ValueError("Metric query exceeds 10000 observations")
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
            if len(values) >= 2:
                result = sum(
                    current - previous if current >= previous else current
                    for previous, current in zip(values, values[1:], strict=False)
                )
    coverage_ratio = None
    if policy["kind"] == "time_weighted":
        next_observed = {
            row.id: rows[index + 1].observed_at if index + 1 < len(rows) else None
            for index, row in enumerate(rows)
        }

        def interval_end(row):
            if row.effective_end is not None:
                return row.effective_end
            following = next_observed[row.id]
            maximum = row.observed_at + timedelta(seconds=policy["max_gap_seconds"])
            return min(following, maximum) if following is not None else maximum

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
        if method == "mean" and rows:
            weighted = [
                (
                    max(
                        0,
                        (
                            min(interval_end(row), end)
                            - max(row.effective_start or row.observed_at, start)
                        ).total_seconds(),
                    ),
                    row.value,
                )
                for row in rows
            ]
            denominator = sum(seconds for seconds, _ in weighted)
            result = (
                sum(seconds * value for seconds, value in weighted) / denominator
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
        "source_refs": [str(row.source_ref) for row in rows],
        "source_revisions": {
            str(row.source_ref): row.projection_version
            for row in rows
            if getattr(row, "source_entry_id", None) is not None
            and getattr(row, "projection_version", None) is not None
        },
        "knowledge_cutoff": knowledge_cutoff.isoformat(),
        "latest_known_at": max((row.ingested_at.isoformat() for row in rows), default=None),
    }
