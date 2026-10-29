"""Permission-filtered discovery over the existing versioned registries."""

from uuid import UUID

from sqlalchemy import func, select

from garmin_ai.accounts import owner
from garmin_ai.models import (
    Event,
    EventDefinitionVersion,
    EventMetricMapping,
    Measurement,
    MetricDefinition,
    MetricDefinitionVersion,
    MetricObservation,
    SourcePayload,
)


def _version_allowed(session, identities, categories):
    from garmin_ai.share_policy import version_sharing_allowed

    if not identities:
        return False
    destination = session.info.get("model_provider_instance_id", "model:gemini:primary")
    channel = session.info.get("channel_destination_instance_id")
    return all(
        version_sharing_allowed(
            session,
            identity,
            destination_kind="model",
            destination_instance_id=destination,
            categories=categories,
        )
        and (
            channel is None
            or version_sharing_allowed(
                session,
                identity,
                destination_kind="channel",
                destination_instance_id=channel,
                categories=categories,
            )
        )
        for identity in identities
    )


def _metric_versions(session, definition):
    return set(
        session.scalars(
            select(EventDefinitionVersion.id)
            .join(
                EventMetricMapping,
                EventMetricMapping.event_definition_version_id == EventDefinitionVersion.id,
            )
            .join(
                MetricDefinitionVersion,
                MetricDefinitionVersion.id == EventMetricMapping.metric_definition_version_id,
            )
            .where(MetricDefinitionVersion.definition_id == definition.id)
        )
    )


def _metric_availability(session, version):
    count, last = session.execute(
        select(
            func.count(),
            func.max(
                func.coalesce(MetricObservation.observed_at, MetricObservation.effective_start)
            ),
        ).where(
            MetricObservation.metric_definition_version_id == version.id,
            MetricObservation.valid.is_(True),
        )
    ).one()
    measurements, last_measurement = session.execute(
        select(func.count(), func.max(Measurement.ts)).where(
            Measurement.metric_definition_version_id == version.id,
            Measurement.quality == "observed",
        )
    ).one()
    timestamps = [value for value in (last, last_measurement) if value is not None]
    return {
        "canonical_observations": count,
        "measurements": measurements,
        "latest_at": max(timestamps).isoformat() if timestamps else None,
        "quality": "valid observations only; completeness is not implied",
        "sources": [
            source
            for source, present in (
                ("canonical_observations", count),
                ("measurements", measurements),
            )
            if present
        ],
    }


def _metrics(session, kind, after_key, resource_key, limit, detail):
    from garmin_ai.scenario_packs import pack_enabled
    from garmin_ai.share_policy import track_channel_share
    from garmin_ai.tools import model_metric_packs

    namespace = "system" if kind == "system_metrics" else "user"
    model = bool(session.info.get("llm_access"))
    person = owner(session)
    visible = []
    cursor = after_key
    while len(visible) <= limit:
        query = (
            select(MetricDefinition, MetricDefinitionVersion)
            .join(
                MetricDefinitionVersion,
                (MetricDefinitionVersion.definition_id == MetricDefinition.id)
                & (MetricDefinitionVersion.version == MetricDefinition.current_version),
            )
            .where(
                MetricDefinition.namespace == namespace,
                MetricDefinition.status == "active",
                MetricDefinition.owner_id == person.id if namespace == "user" else True,
            )
            .order_by(MetricDefinition.key)
            .limit(50)
        )
        if resource_key is not None:
            query = query.where(MetricDefinition.key == resource_key)
        elif cursor is not None:
            query = query.where(MetricDefinition.key > cursor)
        batch = session.execute(query).all()
        if not batch:
            break
        for definition, version in batch:
            identities = (
                _metric_versions(session, definition) if model and namespace == "user" else set()
            )
            if (
                model
                and namespace == "user"
                and not _version_allowed(session, identities, {"schema"})
            ):
                continue
            if (
                model
                and namespace == "system"
                and any(
                    not pack_enabled(session, pack, "llm")
                    for pack in model_metric_packs(definition.key)
                )
            ):
                continue
            facts_allowed = (
                not model
                or namespace == "system"
                or _version_allowed(session, identities, {"facts"})
            )
            row = {
                "id": definition.key,
                "version": version.version,
                "version_id": str(version.id),
                "kind": "metric",
                "labels": version.labels,
                "semantic_type": version.value_kind,
                "unit": version.unit,
                "time_semantics": version.time_semantics,
                "operations": sorted(version.allowed_methods),
                "availability": _metric_availability(session, version) if facts_allowed else None,
            }
            if detail:
                row.update(
                    dimension=version.dimension,
                    scale_id=version.scale_id,
                    scale_version=version.scale_version,
                    aggregation=version.aggregation,
                    coverage_policy=version.coverage_policy,
                    minimum=version.minimum,
                    maximum=version.maximum,
                    category_domain=version.category_domain,
                    schema_hash=version.schema_hash,
                )
            visible.append((row, identities, facts_allowed))
            if len(visible) > limit:
                break
        cursor = batch[-1][0].key
        if resource_key is not None or len(batch) < 50:
            break
    selected = visible[:limit]
    if model and session.info.get("channel_destination_instance_id"):
        for _, identities, facts_allowed in selected:
            for identity in identities:
                track_channel_share(
                    session, identity, {"schema", "facts"} if facts_allowed else {"schema"}
                )
    return {
        "resources": [item[0] for item in selected],
        "next_cursor": selected[-1][0]["id"] if len(visible) > limit else None,
    }


def _events(session, after_key, resource_key, limit, detail):
    from garmin_ai.scenario_packs import event_pack, pack_enabled
    from garmin_ai.share_policy import track_channel_share
    from garmin_ai.tools import event_definitions

    visible = []
    cursor = after_key
    while len(visible) <= limit:
        page = event_definitions(session, after_key=cursor, definition_key=resource_key, limit=50)
        for row in page["rows"]:
            version = row["contract"]
            if version is None or row["status"] != "active":
                continue
            if session.info.get("llm_access") and row["namespace"] == "system":
                pack = event_pack(row["key"].removeprefix("system."))
                if pack is not None and not pack_enabled(session, pack, "llm"):
                    continue
            version_id = UUID(version["id"])
            facts_allowed = (
                not session.info.get("llm_access")
                or row["namespace"] == "system"
                or _version_allowed(session, {version_id}, {"facts"})
            )
            availability = None
            if facts_allowed:
                count, latest = session.execute(
                    select(func.count(), func.max(Event.start)).where(
                        Event.definition_version_id == version_id, Event.deleted.is_(False)
                    )
                ).one()
                availability = {
                    "records_for_version": count,
                    "latest_start": latest.isoformat() if latest is not None else None,
                    "quality": "nondeleted entries; status and completeness may vary",
                }
            descriptor = {
                "id": row["key"],
                "version": version["version"],
                "version_id": version["id"],
                "kind": "event",
                "labels": version["labels"],
                "time_semantics": version["topology"],
                "operations": ["query"] if "query" in version["allowed_operations"] else [],
                "availability": availability,
            }
            if detail:
                descriptor.update(fields=version["fields"], schema_hash=version["schema_hash"])
            visible.append((descriptor, version_id, facts_allowed and row["namespace"] == "user"))
            if len(visible) > limit:
                break
        cursor = page["next_cursor"]
        if cursor is None or resource_key is not None:
            break
    selected = visible[:limit]
    if session.info.get("llm_access") and session.info.get("channel_destination_instance_id"):
        for _, identity, facts_allowed in selected:
            if facts_allowed:
                track_channel_share(session, identity, {"schema", "facts"})
    return {
        "resources": [item[0] for item in selected],
        "next_cursor": selected[-1][0]["id"] if len(visible) > limit else None,
    }


def list_resources(session, *, kind, after_key, resource_key, limit, detail):
    if not 1 <= limit <= 50:
        raise ValueError("Resource page limit must be 1 to 50")
    if resource_key is not None and after_key is not None:
        raise ValueError("Resource key and page cursor are mutually exclusive")
    if detail and resource_key is None:
        raise ValueError("Resource details require a key")
    if kind == "events":
        return _events(session, after_key, resource_key, limit, detail)
    if kind in {"system_metrics", "tracker_metrics"}:
        return _metrics(session, kind, after_key, resource_key, limit, detail)
    present = session.scalar(
        select(func.count())
        .select_from(SourcePayload)
        .where(SourcePayload.source.startswith("plugin:"), SourcePayload.status == "raw_only")
    )
    key = "source.raw_only"
    rows = (
        [{"id": key, "kind": "source", "availability": "raw_only", "operations": []}]
        if present
        and (resource_key is None or resource_key == key)
        and (after_key is None or after_key < key)
        else []
    )
    return {"resources": rows, "next_cursor": None}
