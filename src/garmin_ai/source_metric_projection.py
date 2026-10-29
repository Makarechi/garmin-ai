"""Project confirmed source records into bounded numeric metric observations."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from garmin_ai.accounts import owner
from garmin_ai.metric_definitions import _typed_value, record_observation
from garmin_ai.models import AppState, MetricDefinitionVersion, MetricObservation
from garmin_ai.source_metric_mapping import (
    MAPPING_PREFIX,
    SourceMetricMappingDraft,
    _digest,
    _resolve,
)

SOURCE_FEATURE_VERSION = "source-plugin-v1"


def has_source_observations(session, metric_key):
    """Include invalidated history: as-of queries can still read old observations."""
    return bool(
        session.scalar(
            select(MetricObservation.id)
            .where(
                MetricObservation.metric == metric_key,
                MetricObservation.feature_version == SOURCE_FEATURE_VERSION,
            )
            .limit(1)
        )
    )


PROJECTION_PREFIX = "source-metric-projection:"
MAX_MAPPINGS_PER_SOURCE = 1000


def _path(payload, keys):
    value = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ValueError("Mapped field is missing")
        value = value[key]
    return value


def _value(record, draft, version, time_semantics):
    if time_semantics != "instant" or record.effective_end is not None:
        raise ValueError("Source time semantics are not a point")
    unit = _path(record.payload, draft.unit_path)
    value = _path(record.payload, draft.value_path)
    if not isinstance(unit, str) or unit != draft.unit:
        raise ValueError("Source unit does not match")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Source value is not numeric")
    if (isinstance(value, int) and abs(value) > 2**53) or not math.isfinite(value):
        raise ValueError("Source value is not exactly representable")
    _typed_value(version, value)
    return value


def project_source_record(
    session, settings, instance_id, record, raw_id, revision, fetched_at, time_semantics
):
    """Project one record revision atomically with its raw record and cursor."""
    owner_id = str(owner(session).id)
    mappings = session.scalars(
        select(AppState)
        .where(
            AppState.key.startswith(MAPPING_PREFIX),
            AppState.value["owner_id"].as_string() == owner_id,
            AppState.value["mapping"]["source_instance_id"].as_string() == instance_id,
            AppState.value["state"].as_string().in_(["confirmed_not_normalized", "normalized"]),
        )
        .order_by(AppState.key)
        .limit(MAX_MAPPINGS_PER_SOURCE + 1)
    ).all()
    if len(mappings) > MAX_MAPPINGS_PER_SOURCE:
        raise ValueError("Source has too many confirmed metric mappings")
    changed = False
    identity = hashlib.sha256(record.source_record_id.encode()).hexdigest()
    for mapping in mappings:
        try:
            draft = SourceMetricMappingDraft.model_validate(mapping.value["mapping"])
            metric, source_hash, mapping_owner = _resolve(
                session, settings, draft, lock_contract=True
            )
        except (KeyError, ValueError, ValidationError):
            continue
        if (
            mapping_owner != owner_id
            or mapping.value.get("source_hash") != source_hash
            or mapping.value.get("metric") != metric
        ):
            continue
        mapping_hash = _digest(draft.model_dump(mode="json"))
        key = PROJECTION_PREFIX + mapping.value["id"] + ":" + mapping_hash + ":" + identity
        state = session.get(AppState, key)
        previous = dict(state.value) if state is not None else None
        if (
            previous is not None
            and previous.get("revision") == revision
            and previous.get("time_semantics") == time_semantics
        ):
            continue
        version = session.get(MetricDefinitionVersion, UUID(metric["version_id"]))
        value = None
        status = "deleted" if record.operation == "delete" else "quarantined"
        if record.operation == "upsert":
            try:
                value = _value(record, draft, version, time_semantics)
                status = "projected"
            except ValueError:
                pass
        transition_at = datetime.now(UTC)
        if previous and previous.get("observation_id"):
            old = session.get(MetricObservation, UUID(previous["observation_id"]))
            if old is not None and old.valid:
                old.valid = False
                old.invalidated_at = transition_at
                changed = True
        observation = None
        generation = int(previous.get("generation", 0)) + 1 if previous else 1
        if status == "projected":
            observation = record_observation(
                session,
                version,
                value,
                observed_at=record.effective_at,
                source_ref=raw_id,
                timezone=record.source_timezone,
                recorded_at=record.observed_at,
                uploaded_at=fetched_at,
                ingested_at=transition_at,
                sequence=generation,
            )
            observation.account = "plugin:" + hashlib.sha256(instance_id.encode()).hexdigest()
            observation.device = None
            observation.feature_version = SOURCE_FEATURE_VERSION
            changed = True
            if mapping.value.get("state") != "normalized":
                mapping.value = {**mapping.value, "state": "normalized"}
        value = {
            "revision": revision,
            "generation": generation,
            "time_semantics": time_semantics,
            "status": status,
            "observation_id": str(observation.id) if observation else None,
        }
        if state is None:
            session.add(AppState(key=key, value=value))
        else:
            state.value = value
    session.flush()
    return changed
