"""Bounded, version-aware analytics for system and user-defined metrics."""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import DateTime, cast, func, or_, select

from garmin_ai.events import StrictModel, event_analytic_eligible, serialize
from garmin_ai.metric_definitions import (
    METHODS,
    UNITS,
    CoveragePolicy,
    MetricSpec,
    aggregate_metric,
    bind_event_field,
    measurement_revision_reference,
    measurement_revision_token,
    measurement_rows_as_of,
    parse_measurement_revision_reference,
    register_metric_definition,
)
from garmin_ai.models import (
    Audit,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
)


class AnalysisSpec(StrictModel):
    operation: Literal[
        "query_entries",
        "query_observations",
        "aggregate_metric",
        "compare_periods",
        "query_completeness",
    ]
    metric_key: str | None = Field(
        default=None, pattern=r"^(?:user|system)\.[a-z][a-z0-9_.-]{0,126}$"
    )
    definition_key: str | None = Field(
        default=None, pattern=r"^(?:user|system)\.[a-z][a-z0-9_.-]{0,126}$"
    )
    start: AwareDatetime
    end: AwareDatetime
    comparison_start: AwareDatetime | None = None
    comparison_end: AwareDatetime | None = None
    method: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    metric_version: int | None = Field(default=None, ge=1)
    limit: int = Field(default=500, ge=1, le=1000)
    knowledge_cutoff: AwareDatetime

    @model_validator(mode="after")
    def bounded(self):
        if self.end <= self.start or self.end - self.start > timedelta(days=366):
            raise ValueError("Analysis window must be positive and no wider than 366 days")
        if self.operation == "query_entries" and self.definition_key is None:
            raise ValueError("Entry query requires a definition key")
        if self.operation != "query_entries" and self.metric_key is None:
            raise ValueError("Metric analysis requires a metric key")
        if self.operation == "compare_periods":
            if self.comparison_start is None or self.comparison_end is None:
                raise ValueError("Period comparison requires a second bounded window")
            if (
                self.comparison_end <= self.comparison_start
                or self.comparison_end - self.comparison_start > timedelta(days=366)
            ):
                raise ValueError("Comparison window must be positive and bounded")
        elif self.comparison_start is not None or self.comparison_end is not None:
            raise ValueError("Comparison window is only valid for compare_periods")
        return self


def register_tracker_metrics(session, draft, event_version):
    """Give every analyzable generated field a versioned metric contract."""

    result = []
    for field in draft.fields:
        if field.kind == "text":
            continue
        field_id = f"user.{draft.key}.{field.key}"
        if field.kind == "choice":
            value_kind, unit, dimension = "nominal", None, "category"
            allowed, aggregation = METHODS[value_kind], "counts"
            scale_id = scale_version = minimum = maximum = None
            category_domain = field.options
        elif field.kind == "boolean":
            value_kind, unit, dimension = "boolean", None, "boolean"
            allowed, aggregation = METHODS[value_kind], "rate"
            scale_id = scale_version = minimum = maximum = None
            category_domain = None
        elif field.kind == "scale":
            value_kind = "ordinal"
            unit = f"score_{int(field.minimum)}-{int(field.maximum)}"
            dimension = "ordinal"
            allowed, aggregation = METHODS[value_kind], "median"
            scale_id, scale_version = field_id, 1
            minimum, maximum = field.minimum, field.maximum
            category_domain = None
        else:
            value_kind = "physical_number"
            unit = field.unit or "count"
            dimension = UNITS[unit][0]
            allowed, aggregation = METHODS[value_kind], "mean"
            scale_id = scale_version = None
            minimum, maximum = field.minimum, field.maximum
            category_domain = None
        metric = register_metric_definition(
            session,
            MetricSpec(
                key=field_id,
                labels={draft.locale: field.label},
                value_kind=value_kind,
                unit=unit,
                dimension=dimension,
                scale_id=scale_id,
                scale_version=scale_version,
                aggregation=aggregation,
                allowed_methods=allowed,
                category_domain=category_domain,
                coverage=CoveragePolicy(kind="all_values"),
                time_semantics="point",
                minimum=minimum,
                maximum=maximum,
            ),
            authorized=True,
        )
        bind_event_field(
            session,
            event_version.id,
            field_id,
            metric.id,
            authorized=True,
        )
        result.append(metric)
    return result


def register_definition_metrics(session, spec, event_version):
    """Register and bind metrics for a newly activated tracker definition."""

    def schema_nodes(node):
        if "$ref" in node:
            target = node["$ref"].removeprefix("#/$defs/")
            return schema_nodes(spec.payload_schema["$defs"][target])
        nodes = []
        for keyword in ("oneOf", "anyOf"):
            for choice in node.get(keyword, []):
                nodes.extend(schema_nodes(choice))
        return nodes or [node]

    result = []
    for name, field in spec.fields.items():
        if field.semantic == "text":
            continue
        nodes = [
            node
            for node in schema_nodes(spec.payload_schema["properties"][name])
            if node.get("type") != "null"
        ]
        if field.semantic == "nominal":
            value_kind, unit, dimension = "nominal", None, "category"
            allowed, aggregation = METHODS[value_kind], "counts"
            scale_id = scale_version = minimum = maximum = None
            category_domain = (
                sorted({str(value) for node in nodes for value in node.get("enum", [])}) or None
            )
        elif field.semantic == "boolean":
            value_kind, unit, dimension = "boolean", None, "boolean"
            allowed, aggregation = METHODS[value_kind], "rate"
            scale_id = scale_version = minimum = maximum = None
            category_domain = None
        else:
            minima = [node.get("minimum", node.get("exclusiveMinimum")) for node in nodes]
            maxima = [node.get("maximum", node.get("exclusiveMaximum")) for node in nodes]
            if any(value is None for value in minima + maxima):
                raise ValueError("Numeric tracker fields require bounded schemas")
            minimum, maximum = min(minima), max(maxima)
            unit = field.unit or "count"
            if field.semantic == "ordinal":
                value_kind, dimension = "ordinal", "ordinal"
                allowed, aggregation = METHODS[value_kind], "median"
                scale_id = field.id
                existing = session.scalar(
                    select(MetricDefinition).where(MetricDefinition.key == field.id)
                )
                current = (
                    session.scalar(
                        select(MetricDefinitionVersion).where(
                            MetricDefinitionVersion.definition_id == existing.id,
                            MetricDefinitionVersion.version == existing.current_version,
                        )
                    )
                    if existing is not None
                    else None
                )
                scale_version = (
                    current.scale_version
                    if current is not None
                    and current.unit == unit
                    and current.minimum == minimum
                    and current.maximum == maximum
                    else (current.scale_version or 0) + 1
                    if current is not None
                    else 1
                )
            else:
                value_kind = "physical_number"
                dimension = UNITS[unit][0]
                allowed, aggregation = METHODS[value_kind], "mean"
                scale_id = scale_version = None
            category_domain = None
        metric = register_metric_definition(
            session,
            MetricSpec(
                key=field.id,
                labels=field.labels,
                value_kind=value_kind,
                unit=unit,
                dimension=dimension,
                scale_id=scale_id,
                scale_version=scale_version,
                aggregation=aggregation,
                allowed_methods=allowed,
                category_domain=category_domain,
                coverage=CoveragePolicy(kind="all_values"),
                time_semantics="point",
                minimum=minimum,
                maximum=maximum,
            ),
            authorized=True,
        )
        bind_event_field(session, event_version.id, field.id, metric.id, authorized=True)
        result.append(metric)
    return result


def spec_hash(spec: AnalysisSpec) -> str:
    return hashlib.sha256(
        json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _contract(session, key, version=None):
    definition = session.scalar(select(MetricDefinition).where(MetricDefinition.key == key))
    if definition is None:
        raise LookupError("Metric definition not found")
    contract = session.scalar(
        select(MetricDefinitionVersion).where(
            MetricDefinitionVersion.definition_id == definition.id,
            MetricDefinitionVersion.version == (version or definition.current_version),
        )
    )
    if contract is None:
        raise LookupError("Metric version not found")
    return definition, contract


def query_entries(session, spec: AnalysisSpec):
    definition = session.scalar(
        select(EventDefinition).where(EventDefinition.key == spec.definition_key)
    )
    if definition is None:
        raise LookupError("Event definition not found")
    definition_version_ids = list(
        session.scalars(
            select(EventDefinitionVersion.id).where(
                EventDefinitionVersion.definition_id == definition.id
            )
        )
    )
    definition_version_text = [str(version_id) for version_id in definition_version_ids]
    before_start = cast(Audit.before["start"].as_string(), DateTime(timezone=True))
    after_start = cast(Audit.after["start"].as_string(), DateTime(timezone=True))
    audit_start_in_window = select(Audit.id).where(
        Audit.event_id == Event.id,
        or_(
            (before_start >= spec.start) & (before_start < spec.end),
            (after_start >= spec.start) & (after_start < spec.end),
        ),
    )
    audit_definition_matches = select(Audit.id).where(
        Audit.event_id == Event.id,
        or_(
            Audit.before["definition_version_id"].as_string().in_(definition_version_text),
            Audit.after["definition_version_id"].as_string().in_(definition_version_text),
        ),
    )
    events = session.scalars(
        select(Event)
        .where(
            or_(
                Event.definition_version_id.in_(definition_version_ids),
                audit_definition_matches.exists(),
            ),
            or_(
                (Event.start >= spec.start) & (Event.start < spec.end),
                audit_start_in_window.exists(),
            ),
        )
        .order_by(Event.id)
        .limit(10001)
    ).all()
    if len(events) > 10000:
        raise ValueError("Entry history exceeds its bounded reconstruction limit")
    event_ids = [row.id for row in events]
    latest_known = (
        session.scalars(
            select(Audit)
            .where(
                Audit.event_id.in_(event_ids),
                Audit.created_at <= spec.knowledge_cutoff,
            )
            .distinct(Audit.event_id)
            .order_by(Audit.event_id, Audit.created_at.desc(), Audit.id.desc())
        ).all()
        if event_ids
        else []
    )
    first_future = (
        session.scalars(
            select(Audit)
            .where(
                Audit.event_id.in_(event_ids),
                Audit.created_at > spec.knowledge_cutoff,
            )
            .distinct(Audit.event_id)
            .order_by(Audit.event_id, Audit.created_at, Audit.id)
        ).all()
        if event_ids
        else []
    )
    snapshots = {audit.event_id: audit.after for audit in latest_known}
    future_before = {audit.event_id: audit.before for audit in first_future}
    rows = []
    for event in events:
        snapshot = snapshots.get(event.id, future_before.get(event.id))
        if snapshot is None and event.ingested_at <= spec.knowledge_cutoff:
            if event.updated_at <= spec.knowledge_cutoff:
                snapshot = serialize(event)
            else:
                raise ValueError("Historical entry state is unavailable at this cutoff")
        if snapshot is None or snapshot.get("deleted"):
            continue
        start = datetime.fromisoformat(snapshot["start"])
        if not spec.start <= start < spec.end:
            continue
        version_id = snapshot.get("definition_version_id")
        version = session.get(EventDefinitionVersion, UUID(version_id)) if version_id else None
        if (
            version is None
            or version.definition_id != definition.id
            or "query" not in version.allowed_operations
        ):
            continue
        rows.append(snapshot)
    rows.sort(key=lambda row: (row["start"], row["id"]))
    if len(rows) > spec.limit:
        raise ValueError("Entry query exceeds its explicit result limit")
    return {
        "spec_hash": spec_hash(spec),
        "definition_key": definition.key,
        "rows": [
            {
                "id": row["id"],
                "definition_version_id": row.get("definition_version_id"),
                "revision": row["revision"],
                "start": row["start"],
                "end": row.get("end"),
                "payload": row["payload"],
                "status": row.get("status"),
                "validation_status": row.get("validation_status"),
                "assertion_kind": row.get("assertion_kind"),
                "source": row.get("source"),
                "topology": row.get("topology"),
            }
            for row in rows
        ],
        "knowledge_cutoff": spec.knowledge_cutoff.isoformat(),
    }


def query_observations(session, spec: AnalysisSpec):
    _definition, contract = _contract(session, spec.metric_key, spec.metric_version)
    observation_rows = session.scalars(
        select(MetricObservation)
        .where(
            MetricObservation.metric_definition_version_id == contract.id,
            MetricObservation.observed_at >= spec.start,
            MetricObservation.observed_at < spec.end,
            MetricObservation.ingested_at <= spec.knowledge_cutoff,
            MetricObservation.quality == "observed",
            (MetricObservation.valid.is_(True))
            | (MetricObservation.invalidated_at > spec.knowledge_cutoff),
            or_(
                MetricObservation.source_entry_id.is_(None),
                MetricObservation.source_entry_id.in_(
                    select(Event.id).where(event_analytic_eligible(spec.knowledge_cutoff))
                ),
            ),
        )
        .order_by(MetricObservation.observed_at, MetricObservation.id)
        .limit(spec.limit + 1)
    ).all()
    event_ids = {row.source_entry_id for row in observation_rows if row.source_entry_id}
    events = {row.id: row for row in session.scalars(select(Event).where(Event.id.in_(event_ids)))}
    known_audits = (
        session.scalars(
            select(Audit)
            .where(Audit.event_id.in_(event_ids), Audit.created_at <= spec.knowledge_cutoff)
            .distinct(Audit.event_id)
            .order_by(Audit.event_id, Audit.created_at.desc(), Audit.id.desc())
        ).all()
        if event_ids
        else []
    )
    event_snapshots = {audit.event_id: audit.after for audit in known_audits}
    for event_id, event in events.items():
        if event_id not in event_snapshots and event.updated_at <= spec.knowledge_cutoff:
            event_snapshots[event_id] = serialize(event)
    measurement_rows = measurement_rows_as_of(
        session,
        contract.id,
        spec.start,
        spec.end,
        spec.knowledge_cutoff,
        limit=spec.limit + 1,
    )
    rows = [
        {
            "id": str(row.id),
            "observed_at": row.observed_at,
            "value": row.value
            if row.value is not None
            else row.value_text
            if row.value_text is not None
            else row.value_boolean,
            "source_ref": str(row.source_ref),
            "projection_version": row.projection_version,
            "quality": row.quality,
            "owner_confirmation": (
                event_snapshots[row.source_entry_id].get("status")
                if row.source_entry_id in event_snapshots
                else None
            ),
            "validation_status": (
                event_snapshots[row.source_entry_id].get("validation_status")
                if row.source_entry_id in event_snapshots
                else None
            ),
            "assertion_kind": (
                event_snapshots[row.source_entry_id].get("assertion_kind")
                if row.source_entry_id in event_snapshots
                else None
            ),
            "topology": (
                event_snapshots[row.source_entry_id].get("topology")
                if row.source_entry_id in event_snapshots
                else None
            ),
        }
        for row in observation_rows
    ]
    rows.extend(
        {
            "id": f"measurement:{row.metric}:{row.source}:{row.ts.isoformat()}",
            "observed_at": row.ts,
            "value": row.value,
            "source_ref": str(row.source_ref) if row.source_ref is not None else None,
            "projection_version": None,
            "quality": row.quality,
            "owner_confirmation": None,
            "validation_status": None,
            "assertion_kind": None,
            "topology": None,
        }
        for row in measurement_rows
    )
    rows.sort(key=lambda row: (row["observed_at"], row["id"]))
    if len(rows) > spec.limit:
        raise ValueError("Observation query exceeds its explicit result limit")
    return {
        "spec_hash": spec_hash(spec),
        "metric": spec.metric_key,
        "metric_version": contract.version,
        "unit": contract.unit,
        "scale_id": contract.scale_id,
        "scale_version": contract.scale_version,
        "category_domain": contract.category_domain,
        "rows": [
            {
                **row,
                "observed_at": row["observed_at"].isoformat(),
            }
            for row in rows
        ],
        "knowledge_cutoff": spec.knowledge_cutoff.isoformat(),
    }


def run_aggregate(session, spec: AnalysisSpec):
    result = aggregate_metric(
        session,
        spec.metric_key,
        spec.start,
        spec.end,
        method=spec.method,
        version=spec.metric_version,
        knowledge_cutoff=spec.knowledge_cutoff,
    )
    revisions = result.pop("source_revisions", {})
    generation = result.pop("projection_generation", None)
    return {
        **result,
        "spec_hash": spec_hash(spec),
        "projection_generation": generation,
        "input_revisions": revisions,
        "limitations": [
            "association only; no causal inference",
            "only the selected version, scale, method, and cutoff are comparable",
        ],
    }


def compare_periods(session, spec: AnalysisSpec):
    first = run_aggregate(session, spec.model_copy(update={"operation": "aggregate_metric"}))
    second_spec = spec.model_copy(
        update={
            "operation": "aggregate_metric",
            "start": spec.comparison_start,
            "end": spec.comparison_end,
            "comparison_start": None,
            "comparison_end": None,
        }
    )
    second = run_aggregate(session, second_spec)
    comparable = all(
        first[key] == second[key]
        for key in ("metric_version", "unit", "scale_id", "scale_version", "method")
    )
    numeric = isinstance(first["value"], (int, float)) and isinstance(second["value"], (int, float))
    return {
        "spec_hash": spec_hash(spec),
        "comparable": comparable,
        "first": first,
        "second": second,
        "difference": second["value"] - first["value"] if comparable and numeric else None,
    }


def query_completeness(session, spec: AnalysisSpec):
    aggregate = run_aggregate(session, spec.model_copy(update={"operation": "aggregate_metric"}))
    return {
        "spec_hash": spec_hash(spec),
        "metric": spec.metric_key,
        "observations": aggregate["observations"],
        "coverage_ratio": aggregate["coverage_ratio"],
        "complete": aggregate["value"] is not None,
        "knowledge_cutoff": spec.knowledge_cutoff.isoformat(),
    }


def execute_analysis(session, spec: AnalysisSpec):
    if spec.operation == "query_entries":
        return query_entries(session, spec)
    if spec.operation == "query_observations":
        return query_observations(session, spec)
    if spec.operation == "aggregate_metric":
        return run_aggregate(session, spec)
    if spec.operation == "compare_periods":
        return compare_periods(session, spec)
    return query_completeness(session, spec)


def evidence_is_stale(session, evidence: dict) -> bool:
    try:
        _definition, contract = _contract(
            session,
            evidence["metric"],
            evidence.get("metric_version"),
        )
    except (KeyError, LookupError):
        return True
    for reference, revision in evidence.get("input_revisions", {}).items():
        measurement_identity = parse_measurement_revision_reference(reference)
        if measurement_identity is not None:
            timestamp, metric, source = measurement_identity
            current = measurement_rows_as_of(
                session,
                contract.id,
                timestamp,
                timestamp + timedelta(microseconds=1),
                datetime.now(timestamp.tzinfo),
                limit=None,
            )
            matching = next(
                (
                    row
                    for row in current
                    if row.metric == metric and row.source == source and row.ts == timestamp
                ),
                None,
            )
            if (
                matching is None
                or measurement_revision_reference(matching) != reference
                or measurement_revision_token(matching) != revision
            ):
                return True
            continue
        event = session.get(Event, UUID(reference))
        current_projection = session.scalar(
            select(func.max(MetricObservation.projection_version)).where(
                MetricObservation.source_entry_id == UUID(reference),
                MetricObservation.metric_definition_version_id == contract.id,
                MetricObservation.valid.is_(True),
            )
        )
        if event is None or event.deleted or current_projection != revision:
            return True
    return False


class DimensionedValue(StrictModel):
    value: float
    dimensions: dict[str, int] = Field(default_factory=dict)


def evaluate_formula(expression: str, values: dict[str, DimensionedValue]) -> DimensionedValue:
    """Evaluate a tiny arithmetic AST; no calls, attributes, subscripts, or code execution."""

    if len(expression) > 300 or len(values) > 20:
        raise ValueError("Derived formula exceeds resource limits")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ValueError("Invalid derived formula") from None
    nodes = list(ast.walk(tree))
    if len(nodes) > 40:
        raise ValueError("Derived formula exceeds resource limits")
    allowed = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Name,
        ast.Constant,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.USub,
        ast.UAdd,
        ast.Load,
    )
    if any(not isinstance(node, allowed) for node in nodes):
        raise ValueError("Derived formula contains a forbidden operation")

    def calculate(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("Formula constants must be numeric")
            return DimensionedValue(value=float(node.value))
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ValueError("Formula references an unknown metric alias")
            return values[node.id]
        if isinstance(node, ast.UnaryOp):
            value = calculate(node.operand)
            return value.model_copy(
                update={"value": -value.value if isinstance(node.op, ast.USub) else value.value}
            )
        if isinstance(node, ast.BinOp):
            left, right = calculate(node.left), calculate(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if left.dimensions != right.dimensions:
                    raise ValueError("Formula adds values with incompatible dimensions")
                result = (
                    left.value + right.value
                    if isinstance(node.op, ast.Add)
                    else left.value - right.value
                )
                return DimensionedValue(value=result, dimensions=left.dimensions)
            dimensions = dict(left.dimensions)
            direction = 1 if isinstance(node.op, ast.Mult) else -1
            for name, power in right.dimensions.items():
                dimensions[name] = dimensions.get(name, 0) + direction * power
                if dimensions[name] == 0:
                    del dimensions[name]
            if isinstance(node.op, ast.Div) and right.value == 0:
                raise ValueError("Formula divides by zero")
            result = (
                left.value * right.value
                if isinstance(node.op, ast.Mult)
                else left.value / right.value
            )
            return DimensionedValue(value=result, dimensions=dimensions)
        raise ValueError("Invalid derived formula")

    return calculate(tree.body)
