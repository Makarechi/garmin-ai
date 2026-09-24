"""Read-only preview of custom event projection drift."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from garmin_ai.metric_definitions import _typed_value
from garmin_ai.models import (
    Audit,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    EventMetricMapping,
    MetricDefinitionVersion,
    MetricObservation,
)


def preview_custom_projection_drift(session, *, limit=500, after_event_id: UUID | None = None):
    """Compare current facts with active projections without changing either side."""
    if not 1 <= limit <= 1000:
        raise ValueError("Projection audit limit must be between 1 and 1000")
    query = (
        select(Event)
        .join(EventDefinitionVersion, Event.definition_version_id == EventDefinitionVersion.id)
        .join(EventDefinition, EventDefinitionVersion.definition_id == EventDefinition.id)
        .where(EventDefinition.namespace == "user")
        .order_by(Event.id)
        .limit(limit + 1)
    )
    if after_event_id is not None:
        query = query.where(Event.id > after_event_id)
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
            for field_id, mapping in latest.items():
                value = event.payload.get(fields[field_id])
                if value is None:
                    continue
                metric_version = session.get(
                    MetricDefinitionVersion, mapping.metric_definition_version_id
                )
                number, text, boolean = _typed_value(metric_version, value)
                expected[field_id] = (
                    metric_version.id,
                    number,
                    text,
                    boolean,
                    metric_version.unit or "1",
                    event.id,
                    event.start,
                    event.start,
                    event.end,
                    event.timezone,
                )
        actual = session.scalars(
            select(MetricObservation).where(
                MetricObservation.source_entry_id == event.id,
                MetricObservation.valid.is_(True),
            )
        ).all()
        by_field = {}
        for observation in actual:
            by_field.setdefault(observation.field_id, []).append(observation)
        missing = stale = mismatched = 0
        for field_id, target in expected.items():
            current = by_field.get(field_id, [])
            if not current:
                missing += 1
                continue
            if (
                len(current) != 1
                or (
                    current[0].metric_definition_version_id,
                    current[0].value,
                    current[0].value_text,
                    current[0].value_boolean,
                    current[0].unit,
                    current[0].source_ref,
                    current[0].observed_at,
                    current[0].effective_start,
                    current[0].effective_end,
                    current[0].timezone,
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
        history_unknown = not audits or any(
            audit.after is None or "status" not in audit.after for audit in audits
        )
        summary = {
            "event_id": str(event.id),
            "expected": len(expected),
            "valid": len(actual),
            "missing": missing,
            "stale": stale,
            "mismatched": mismatched,
            "history_unknown": history_unknown,
            "pending": event.status != "confirmed",
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
        "next_cursor": str(page[-1].id) if more else None,
        "writes": False,
    }
