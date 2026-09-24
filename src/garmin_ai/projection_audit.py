"""Read-only preview of custom event projection drift."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, tuple_
from sqlalchemy import text as sql_text

from garmin_ai.metric_definitions import _typed_value, event_projection_value
from garmin_ai.models import (
    Audit,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    EventMetricMapping,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
)


def _cursor_value(raw, snapshot):
    if raw is None:
        return datetime.now(UTC), None
    if len(raw) > 4096:
        raise ValueError("Invalid projection audit cursor")
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if payload["snapshot"] != snapshot:
            raise ValueError("Projection audit cursor requires its original snapshot")
        bound = datetime.fromisoformat(payload["bound"])
        position = (datetime.fromisoformat(payload["at"]), UUID(payload["id"]))
        if bound.tzinfo is None or position[0].tzinfo is None:
            raise ValueError
        return bound, position
    except (KeyError, TypeError, ValueError, binascii.Error, UnicodeDecodeError):
        raise ValueError("Invalid projection audit cursor") from None


def _next_cursor(snapshot, bound, event):
    payload = {
        "snapshot": snapshot,
        "bound": bound.isoformat(),
        "at": event.ingested_at.isoformat(),
        "id": str(event.id),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def preview_custom_projection_drift(session, *, limit=500, cursor: str | None = None):
    """Compare current facts with active projections without changing either side."""
    if not 1 <= limit <= 1000:
        raise ValueError("Projection audit limit must be between 1 and 1000")
    snapshot = session.scalar(sql_text("SELECT txid_current_snapshot()::text"))
    bound, position = _cursor_value(cursor, snapshot)
    query = (
        select(Event)
        .join(EventDefinitionVersion, Event.definition_version_id == EventDefinitionVersion.id)
        .join(EventDefinition, EventDefinitionVersion.definition_id == EventDefinition.id)
        .where(EventDefinition.namespace == "user", Event.ingested_at <= bound)
        .order_by(Event.ingested_at, Event.id)
        .limit(limit + 1)
    )
    if position is not None:
        query = query.where(tuple_(Event.ingested_at, Event.id) > position)
    events = session.scalars(query).all()
    page, more = events[:limit], len(events) > limit
    totals = {
        "events": 0,
        "expected": 0,
        "valid": 0,
        "missing": 0,
        "stale": 0,
        "mismatched": 0,
        "history_unknown": 0,
        "pending": 0,
    }
    rows = []
    for event in page:
        version = session.get(EventDefinitionVersion, event.definition_version_id)
        fields = {metadata["id"]: name for name, metadata in version.field_metadata.items()}
        mappings = session.scalars(
            select(EventMetricMapping)
            .where(EventMetricMapping.event_definition_version_id == version.id)
            .order_by(EventMetricMapping.field_id, EventMetricMapping.projection_version.desc())
        ).all()
        latest = {}
        for mapping in mappings:
            latest.setdefault(mapping.field_id, mapping)
        expected = {}
        if not event.deleted:
            for sequence, (field_id, mapping) in enumerate(latest.items()):
                value = event_projection_value(event, field_id, fields)
                if value is None:
                    continue
                metric_version = session.get(
                    MetricDefinitionVersion, mapping.metric_definition_version_id
                )
                number, text, boolean = _typed_value(metric_version, value)
                metric = session.get(MetricDefinition, metric_version.definition_id)
                expected[field_id] = (
                    metric.key,
                    metric_version.id,
                    number,
                    text,
                    boolean,
                    metric_version.unit or "1",
                    event.id,
                    event.start,
                    event.start,
                    event.end,
                    event.recorded_at,
                    sequence,
                    event.timezone,
                    event.start.astimezone(ZoneInfo(event.timezone)).date(),
                    "observed",
                    "event-projection-v1",
                    None,
                    None,
                    None,
                    None,
                )
        all_observations = session.scalars(
            select(MetricObservation).where(
                MetricObservation.source_entry_id == event.id,
            )
        ).all()
        actual = [observation for observation in all_observations if observation.valid]
        generations = {}
        for observation in all_observations:
            generations.setdefault(observation.field_id, []).append(observation.projection_version)
        by_field = {}
        for observation in actual:
            by_field.setdefault(observation.field_id, []).append(observation)
        missing = stale = mismatched = 0
        for field_id, target in expected.items():
            current = by_field.get(field_id, [])
            if not current:
                missing += 1
                continue
            lineage = generations.get(field_id, [])
            if (
                len(current) != 1
                or any(not isinstance(generation, int) for generation in lineage)
                or sorted(lineage) != list(range(1, len(lineage) + 1))
                or current[0].projection_version != len(lineage)
                or (
                    current[0].metric,
                    current[0].metric_definition_version_id,
                    current[0].value,
                    current[0].value_text,
                    current[0].value_boolean,
                    current[0].unit,
                    current[0].source_ref,
                    current[0].observed_at,
                    current[0].effective_start,
                    current[0].effective_end,
                    current[0].recorded_at,
                    current[0].sequence,
                    current[0].timezone,
                    current[0].source_calendar_date,
                    current[0].quality,
                    current[0].feature_version,
                    current[0].account,
                    current[0].device,
                    current[0].precision,
                    current[0].coverage,
                )
                != target
            ):
                mismatched += 1
        for field_id, observations in by_field.items():
            if field_id not in expected:
                stale += len(observations)
        audits = session.scalars(
            select(Audit).where(Audit.event_id == event.id).order_by(Audit.created_at, Audit.id)
        ).all()
        times = [audit.created_at for audit in audits]
        history_unknown = (
            not audits
            or len(times) != len(set(times))
            or any(audit.after is None or "status" not in audit.after for audit in audits)
        )
        summary = {
            "event_id": str(event.id),
            "expected": len(expected),
            "valid": len(actual),
            "missing": missing,
            "stale": stale,
            "mismatched": mismatched,
            "history_unknown": history_unknown,
            "pending": not event.deleted and event.status != "confirmed",
        }
        rows.append(summary)
        totals["events"] += 1
        for key in ("expected", "valid", "missing", "stale", "mismatched"):
            totals[key] += summary[key]
        totals["history_unknown"] += int(history_unknown)
        totals["pending"] += int(summary["pending"])
    return {
        "totals": totals,
        "rows": rows,
        "next_cursor": _next_cursor(snapshot, bound, page[-1]) if more else None,
        "writes": False,
    }
