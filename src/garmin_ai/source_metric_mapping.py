"""Owner-reviewed source field mappings; no source data is imported here."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

from garmin_ai.accounts import owner
from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.integrations import configured_instances
from garmin_ai.models import (
    AppState,
    EventDefinition,
    EventDefinitionVersion,
    EventMetricMapping,
    MetricDefinition,
    MetricDefinitionVersion,
)
from garmin_ai.onboarding import source_instance_selected

PREVIEW_PREFIX = "source-mapping-preview:"
MAPPING_PREFIX = "source-metric-mapping:"
PathKey = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")]


class SourceMetricMappingDraft(StrictModel):
    source_instance_id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,199}$")
    metric_version_id: UUID
    value_path: list[PathKey] = Field(min_length=1, max_length=8)
    unit_path: list[PathKey] = Field(min_length=1, max_length=8)
    unit: str = Field(pattern=r"^[A-Za-z0-9_%./-]{1,32}$")

    @field_validator("value_path", "unit_path")
    @classmethod
    def valid_unicode(cls, path):
        try:
            for key in path:
                key.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Path keys must be valid Unicode") from None
        return path

    @model_validator(mode="after")
    def distinct_paths(self):
        shared = min(len(self.value_path), len(self.unit_path))
        if self.value_path[:shared] == self.unit_path[:shared]:
            raise ValueError("Value and unit paths must not overlap")
        return self


class SourceMetricMappingConfirmation(StrictModel):
    draft: SourceMetricMappingDraft
    confirmation_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconfirm_source_identity: bool = Field(default=False, strict=True)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _public_mapping(value):
    return {key: value[key] for key in ("id", "state", "mapping", "metric", "confirmed_at")}


def _resolve(session, settings, draft, *, lock_contract=False):
    instances = [
        item for item in configured_instances(settings) if item.id == draft.source_instance_id
    ]
    if (
        len(instances) != 1
        or not instances[0].enabled
        or instances[0].kind != "source"
        or not source_instance_selected(session, draft.source_instance_id)
    ):
        raise ValueError("Source instance is unavailable or not selected")
    person = owner(session)
    row = session.execute(
        select(MetricDefinition, MetricDefinitionVersion)
        .join(MetricDefinitionVersion, MetricDefinitionVersion.definition_id == MetricDefinition.id)
        .where(
            MetricDefinitionVersion.id == draft.metric_version_id,
            MetricDefinitionVersion.version == MetricDefinition.current_version,
            MetricDefinition.owner_id == person.id,
            MetricDefinition.namespace == "user",
            MetricDefinition.status == "active",
        )
    ).one_or_none()
    if row is None:
        raise ValueError("Metric version is unavailable or not current")
    definition, version = row
    linked_query = (
        select(EventDefinition.id)
        .select_from(EventMetricMapping)
        .join(
            EventDefinitionVersion,
            EventDefinitionVersion.id == EventMetricMapping.event_definition_version_id,
        )
        .join(EventDefinition, EventDefinition.id == EventDefinitionVersion.definition_id)
        .where(
            EventMetricMapping.metric_definition_version_id == version.id,
            EventDefinition.owner_id == person.id,
            EventDefinition.status == "active",
            EventDefinitionVersion.version == EventDefinition.current_version,
        )
        .order_by(EventDefinition.id)
        .limit(1)
    )
    if lock_contract:
        linked_query = linked_query.with_for_update(of=EventDefinition, nowait=True)
    linked = session.scalar(linked_query)
    if lock_contract:
        session.refresh(definition, with_for_update={"nowait": True})
    if (
        linked is None
        or definition.current_version != version.version
        or definition.status != "active"
        or version.value_kind != "physical_number"
        or version.time_semantics != "point"
        or not version.unit
        or version.unit != draft.unit
    ):
        raise ValueError("Metric contract is unsupported or unit does not match")
    metric = {
        "id": definition.key,
        "version_id": str(version.id),
        "schema_hash": version.schema_hash,
        "labels": version.labels,
        "value_kind": version.value_kind,
        "dimension": version.dimension,
        "unit": version.unit,
        "minimum": version.minimum,
        "maximum": version.maximum,
        "time_semantics": version.time_semantics,
    }
    return metric, _digest(instances[0].model_dump(mode="json")), str(person.id)


def preview_source_metric_mapping(session, settings, draft):
    draft = SourceMetricMappingDraft.model_validate(draft)
    metric, source_hash, owner_id = _resolve(session, settings, draft)
    identity = _digest([owner_id, draft.source_instance_id, str(draft.metric_version_id)])
    existing = session.get(AppState, MAPPING_PREFIX + identity, populate_existing=True)
    existing_value = dict(existing.value) if existing is not None else None
    reconfirmation = None
    if (
        existing_value is not None
        and existing_value.get("state") == "normalized"
        and existing_value.get("mapping") == draft.model_dump(mode="json")
        and existing_value.get("owner_id") == owner_id
        and existing_value.get("metric") == metric
        and existing_value.get("source_hash") != source_hash
    ):
        reconfirmation = {
            "preserves_record_identity": True,
            "warning": "Existing record IDs will refer to the same history; confirm only if the configured source retains those identities.",
        }
    token = secrets.token_hex(32)
    now = datetime.now(UTC)
    session.execute(
        delete(AppState).where(
            AppState.key.startswith(PREVIEW_PREFIX),
            AppState.value["expires_at"].as_string() < now.isoformat(),
        )
    )
    session.add(
        AppState(
            key=PREVIEW_PREFIX + token,
            value={
                "draft_hash": _digest(draft.model_dump(mode="json")),
                "metric_hash": _digest(metric),
                "source_hash": source_hash,
                "owner_id": owner_id,
                "existing_mapping_hash": _digest(existing_value) if existing_value else None,
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
            },
        )
    )
    session.flush()
    return {
        "confirmation_token": token,
        "mapping": draft.model_dump(mode="json"),
        "metric": metric,
        "reconfirmation": reconfirmation,
    }


def confirm_source_metric_mapping(session, settings, confirmation):
    confirmation = SourceMetricMappingConfirmation.model_validate(confirmation)
    draft = confirmation.draft
    lock_writes(session)
    preview = session.get(
        AppState, PREVIEW_PREFIX + confirmation.confirmation_token, with_for_update=True
    )
    if (
        preview is None
        or datetime.fromisoformat(preview.value["expires_at"]) <= datetime.now(UTC)
        or preview.value.get("draft_hash") != _digest(draft.model_dump(mode="json"))
    ):
        raise Conflict("Source mapping preview changed; preview it again")
    try:
        with session.begin_nested():
            metric, source_hash, owner_id = _resolve(session, settings, draft, lock_contract=True)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise Conflict("Tracker or metric is changing; retry the confirmation") from None
        raise
    if (
        preview.value.get("metric_hash") != _digest(metric)
        or preview.value.get("source_hash") != source_hash
        or preview.value.get("owner_id") != owner_id
    ):
        raise Conflict("Source or metric changed; preview the mapping again")
    identity = _digest([owner_id, draft.source_instance_id, str(draft.metric_version_id)])
    key = MAPPING_PREFIX + identity
    existing = session.get(AppState, key, populate_existing=True)
    data = draft.model_dump(mode="json")
    if existing is not None and (
        existing.value.get("mapping") != data
        or existing.value.get("owner_id") != owner_id
        or existing.value.get("metric") != metric
    ):
        raise Conflict("Source and metric already have a different confirmed mapping")
    if existing is not None and existing.value.get("source_hash") != source_hash:
        if (
            existing.value.get("state") != "normalized"
            or not confirmation.reconfirm_source_identity
            or preview.value.get("existing_mapping_hash") != _digest(dict(existing.value))
        ):
            raise Conflict("Source identity needs a current explicit confirmation")
        confirmed_at = datetime.now(UTC).isoformat()
        session.add(
            AppState(
                key="source-mapping-confirmation:" + secrets.token_hex(32),
                value={
                    "mapping_id": identity,
                    "owner_id": owner_id,
                    "previous_source_hash": existing.value["source_hash"],
                    "previous_confirmed_at": existing.value["confirmed_at"],
                    "source_hash": source_hash,
                    "confirmed_at": confirmed_at,
                },
            )
        )
        existing.value = {
            **existing.value,
            "source_hash": source_hash,
            "confirmed_at": confirmed_at,
        }
    if existing is None:
        value = {
            "id": identity,
            "state": "confirmed_not_normalized",
            "mapping": data,
            "metric": metric,
            "source_hash": source_hash,
            "owner_id": owner_id,
            "confirmed_at": datetime.now(UTC).isoformat(),
        }
        session.add(AppState(key=key, value=value))
    else:
        value = dict(existing.value)
    session.delete(preview)
    session.flush()
    return _public_mapping(value)


def list_source_metric_mappings(session, *, limit=100, after_id=None):
    if not 1 <= limit <= 100:
        raise ValueError("Invalid mapping page limit")
    query = (
        select(AppState)
        .where(
            AppState.key.startswith(MAPPING_PREFIX),
            AppState.value["owner_id"].as_string() == str(owner(session).id),
        )
        .order_by(AppState.key)
    )
    if after_id is not None:
        if len(after_id) != 64 or any(char not in "0123456789abcdef" for char in after_id):
            raise ValueError("Invalid mapping cursor")
        query = query.where(AppState.key > MAPPING_PREFIX + after_id)
    rows = session.scalars(query.limit(limit + 1)).all()
    return {
        "mappings": [_public_mapping(row.value) for row in rows[:limit]],
        "next_cursor": rows[limit - 1].value["id"] if len(rows) > limit else None,
    }


def delete_source_metric_mapping(session, mapping_id):
    if len(mapping_id) != 64 or any(char not in "0123456789abcdef" for char in mapping_id):
        raise ValueError("Invalid mapping ID")
    lock_writes(session)
    row = session.get(AppState, MAPPING_PREFIX + mapping_id, with_for_update=True)
    if row is None or row.value.get("owner_id") != str(owner(session).id):
        raise LookupError("Source mapping not found")
    if row.value.get("state") != "confirmed_not_normalized":
        raise Conflict("Mapping with normalized data cannot be removed here")
    session.delete(row)
    session.flush()
    return {"deleted": True}
