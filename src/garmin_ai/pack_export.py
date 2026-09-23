"""Data-only tracker pack export; never includes owner facts or integration state."""

from uuid import UUID

from sqlalchemy import select

from garmin_ai.models import (
    EventDefinition,
    EventDefinitionVersion,
    EventMetricMapping,
    MetricDefinition,
    MetricDefinitionVersion,
    TrackerConfig,
)


def export_tracker_pack(session, definition_ids: list[UUID]):
    if not definition_ids or len(definition_ids) > 32:
        raise ValueError("Pack must select between one and 32 tracker definitions")
    definitions = session.scalars(
        select(EventDefinition)
        .where(
            EventDefinition.id.in_(definition_ids),
            EventDefinition.namespace == "user",
        )
        .order_by(EventDefinition.key)
    ).all()
    if len(definitions) != len(set(definition_ids)):
        raise LookupError("Tracker definition not found")
    exported = []
    for definition in definitions:
        versions = session.scalars(
            select(EventDefinitionVersion)
            .where(EventDefinitionVersion.definition_id == definition.id)
            .order_by(EventDefinitionVersion.version)
        ).all()
        tracker = session.scalar(
            select(TrackerConfig).where(TrackerConfig.definition_id == definition.id)
        )
        mappings = session.scalars(
            select(EventMetricMapping).where(
                EventMetricMapping.event_definition_version_id.in_([row.id for row in versions])
            )
        ).all()
        metric_versions = [
            session.get(MetricDefinitionVersion, row.metric_definition_version_id)
            for row in mappings
        ]
        metric_definitions = {
            row.definition_id: session.get(MetricDefinition, row.definition_id)
            for row in metric_versions
        }
        exported.append(
            {
                "key": definition.key,
                "status": definition.status,
                "current_version": definition.current_version,
                "versions": [
                    {
                        "version": row.version,
                        "schema": row.schema,
                        "schema_hash": row.schema_hash,
                        "field_metadata": row.field_metadata,
                        "topology": row.topology,
                        "labels": row.labels,
                        "privacy": row.privacy,
                        "allowed_operations": row.allowed_operations,
                    }
                    for row in versions
                ],
                "tracker": (
                    {
                        "shortcut": tracker.shortcut,
                        "reminder_enabled": tracker.reminder_enabled,
                        "reminder_time": tracker.reminder_time,
                        "reminder_timezone": tracker.reminder_timezone,
                    }
                    if tracker
                    else None
                ),
                "metrics": [
                    {
                        "key": metric_definitions[row.definition_id].key,
                        "version": row.version,
                        "value_kind": row.value_kind,
                        "unit": row.unit,
                        "dimension": row.dimension,
                        "scale_id": row.scale_id,
                        "scale_version": row.scale_version,
                        "aggregation": row.aggregation,
                        "coverage_policy": row.coverage_policy,
                        "time_semantics": row.time_semantics,
                        "minimum": row.minimum,
                        "maximum": row.maximum,
                        "labels": row.labels,
                        "allowed_methods": row.allowed_methods,
                        "schema_hash": row.schema_hash,
                    }
                    for row in metric_versions
                ],
                "mappings": [
                    {
                        "event_definition_version": next(
                            version.version
                            for version in versions
                            if version.id == mapping.event_definition_version_id
                        ),
                        "field_id": mapping.field_id,
                        "metric_key": metric_definitions[
                            session.get(
                                MetricDefinitionVersion,
                                mapping.metric_definition_version_id,
                            ).definition_id
                        ].key,
                        "metric_version": session.get(
                            MetricDefinitionVersion,
                            mapping.metric_definition_version_id,
                        ).version,
                        "projection_version": mapping.projection_version,
                    }
                    for mapping in mappings
                ],
            }
        )
    return {"format": "garmin-ai-tracker-pack-v1", "trackers": exported}
