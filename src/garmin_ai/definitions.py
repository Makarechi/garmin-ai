"""Versioned event definitions with a bounded, non-executable schema profile."""

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.accounts import owner
from garmin_ai.models import Audit, Event, EventDefinition, EventDefinitionVersion

KEY = re.compile(r"^user\.[a-z][a-z0-9_]{0,62}$")
FIELD = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
STABLE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
LOCAL_REF = re.compile(r"^#/\$defs/[a-zA-Z][a-zA-Z0-9_-]{0,62}$")
ALLOWED_SCHEMA_KEYS = {
    "$schema",
    "$defs",
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "oneOf",
    "anyOf",
    "title",
    "description",
}
ALLOWED_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
SYSTEM_CONTEXT_KINDS = {
    "alcohol",
    "meal",
    "hydration",
    "illness",
    "nap",
    "stressor",
    "travel",
    "mood",
    "note",
    "context",
    "caffeine_absence",
    "caffeine_log_complete",
}


class DefinitionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, populate_by_name=True, serialize_by_alias=True
    )


class FieldSpec(DefinitionModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    labels: dict[str, str] = Field(min_length=1, max_length=8)
    semantic: Literal["nominal", "ordinal", "count", "quantity", "text", "boolean"]
    unit: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_%./-]{1,32}$")

    @model_validator(mode="after")
    def bounded_labels(self):
        _validate_labels(self.labels)
        return self


class DefinitionSpec(DefinitionModel):
    key: str = Field(pattern=r"^user\.[a-z][a-z0-9_]{0,62}$")
    labels: dict[str, str] = Field(min_length=1, max_length=8)
    payload_schema: dict = Field(alias="schema")
    fields: dict[str, FieldSpec] = Field(min_length=1, max_length=32)
    topology: Literal["point", "open_interval", "bounded_interval", "flexible"]
    privacy: Literal["private", "sensitive"] = "private"
    allowed_operations: set[Literal["create", "update", "delete", "query"]] = Field(
        default_factory=lambda: {"create", "update", "delete", "query"}, min_length=1
    )

    @model_validator(mode="after")
    def valid_contract(self):
        _validate_labels(self.labels)
        validate_schema(self.payload_schema)
        properties = set(self.payload_schema.get("properties", {}))
        if "type" in properties:
            raise ValueError("The payload type discriminator is reserved")
        if properties != set(self.fields):
            raise ValueError("Field metadata must exactly match schema properties")
        identities = [field.id for field in self.fields.values()]
        if len(identities) != len(set(identities)):
            raise ValueError("Field identities must be distinct")
        if any(not STABLE_ID.fullmatch(identity) for identity in identities):
            raise ValueError("Invalid stable field identity")
        return self


class CustomEntryInput(DefinitionModel):
    definition_key: str = Field(pattern=r"^user\.[a-z][a-z0-9_]{0,62}$")
    start: AwareDatetime
    end: AwareDatetime | None = None
    timezone: str = "Europe/Bratislava"
    source: Literal["manual", "telegram_text", "telegram_button", "telegram_voice", "mcp"] = (
        "manual"
    )
    confidence: float = Field(default=1, ge=0, le=1)
    status: Literal["confirmed", "needs_confirmation"] = "confirmed"
    original_text: str | None = Field(default=None, max_length=16000)
    values: dict = Field(max_length=32)
    units: dict[str, str] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def valid_time(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown timezone") from None
        if self.end is not None and self.end < self.start:
            raise ValueError("End must not precede start")
        return self


class DefinitionRevision(DefinitionModel):
    revision: int = Field(ge=1, strict=True)
    spec: DefinitionSpec


class DefinitionActivation(DefinitionModel):
    revision: int = Field(ge=1, strict=True)


def _validate_labels(labels):
    for locale, label in labels.items():
        if not re.fullmatch(r"^[a-z]{2,3}(?:-[A-Z]{2})?$", locale):
            raise ValueError("Invalid label locale")
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise ValueError("Definition labels must be nonempty and bounded")


def _schema_node(node, depth=0):
    if depth > 8 or not isinstance(node, dict):
        raise ValueError("Schema depth or shape exceeds the supported profile")
    unknown = set(node) - ALLOWED_SCHEMA_KEYS
    if unknown:
        raise ValueError("Unsupported schema keyword: " + sorted(unknown)[0])
    if "$ref" in node and (
        not isinstance(node["$ref"], str) or not LOCAL_REF.fullmatch(node["$ref"])
    ):
        raise ValueError("Only local bounded schema references are allowed")
    if "type" in node and node["type"] not in ALLOWED_TYPES:
        raise ValueError("Unsupported schema type")
    if {"properties", "required", "additionalProperties"}.intersection(node) and node.get(
        "type"
    ) != "object":
        raise ValueError("Object schema keywords require type object")
    if node.get("type") == "object" and node.get("additionalProperties") is not False:
        raise ValueError("Every schema object must reject additional properties")
    if node.get("type") == "array" and (
        "items" not in node
        or not isinstance(node.get("maxItems"), int)
        or not 0 <= node.get("minItems", 0) <= node["maxItems"] <= 1000
    ):
        raise ValueError("Arrays require a bounded item count")
    if node.get("type") == "string" and "enum" not in node and "const" not in node:
        maximum = node.get("maxLength")
        minimum = node.get("minLength", 0)
        if (
            not isinstance(minimum, int)
            or not isinstance(maximum, int)
            or not 0 <= minimum <= maximum <= 16000
        ):
            raise ValueError("Strings require a bounded length")
    if node.get("type") in {"integer", "number"}:
        minimum = node.get("minimum", node.get("exclusiveMinimum"))
        maximum = node.get("maximum", node.get("exclusiveMaximum"))
        if (
            isinstance(minimum, bool)
            or isinstance(maximum, bool)
            or not isinstance(minimum, (int, float))
            or not isinstance(maximum, (int, float))
            or not math.isfinite(minimum)
            or not math.isfinite(maximum)
            or minimum > maximum
        ):
            raise ValueError("Numbers require finite lower and upper bounds")
    for key in ("title", "description"):
        if key in node and (not isinstance(node[key], str) or len(node[key]) > 500):
            raise ValueError("Schema text is invalid or too long")
    properties = node.get("properties", {})
    if not isinstance(properties, dict) or len(properties) > 32:
        raise ValueError("Schema properties must be a bounded object")
    for name, child in properties.items():
        if not FIELD.fullmatch(name):
            raise ValueError("Invalid schema field name")
        _schema_node(child, depth + 1)
    definitions = node.get("$defs", {})
    if not isinstance(definitions, dict) or len(definitions) > 16:
        raise ValueError("Schema definitions must be bounded")
    for name, child in definitions.items():
        if not re.fullmatch(r"^[a-zA-Z][a-zA-Z0-9_-]{0,62}$", name):
            raise ValueError("Invalid local schema definition")
        _schema_node(child, depth + 1)
    if "items" in node:
        _schema_node(node["items"], depth + 1)
    for keyword in ("oneOf", "anyOf"):
        if keyword in node:
            choices = node[keyword]
            if not isinstance(choices, list) or not 1 <= len(choices) <= 4:
                raise ValueError("Schema composition must be bounded")
            for child in choices:
                _schema_node(child, depth + 1)
    required = node.get("required", [])
    if (
        not isinstance(required, list)
        or len(required) != len(set(required))
        or any(name not in properties for name in required)
    ):
        raise ValueError("Schema required fields are invalid")
    if "enum" in node and (
        not isinstance(node["enum"], list)
        or not 1 <= len(node["enum"]) <= 50
        or any(isinstance(item, (dict, list)) for item in node["enum"])
    ):
        raise ValueError("Schema enum must be bounded and scalar")


def validate_schema(schema):
    try:
        encoded = json.dumps(
            schema, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (RecursionError, TypeError, ValueError):
        raise ValueError("Schema must be finite JSON") from None
    if len(encoded) > 32768:
        raise ValueError("Schema exceeds 32 KiB")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError("Root schema must be a closed object")
    if schema.get("$schema") not in {None, "https://json-schema.org/draft/2020-12/schema"}:
        raise ValueError("Only JSON Schema Draft 2020-12 is supported")
    _schema_node(schema)
    definitions = schema.get("$defs", {})

    def references(node):
        if isinstance(node, dict):
            if "$ref" in node:
                yield node["$ref"].removeprefix("#/$defs/")
            for value in node.values():
                yield from references(value)
        elif isinstance(node, list):
            for value in node:
                yield from references(value)

    graph = {name: set(references(value)) for name, value in definitions.items()}
    if any(target not in definitions for target in references(schema)):
        raise ValueError("Local schema reference does not exist")

    def visit(name, visiting, visited):
        if name in visiting:
            raise ValueError("Recursive schema references are not allowed")
        if name in visited:
            return
        visiting.add(name)
        for target in graph[name]:
            visit(target, visiting, visited)
        visiting.remove(name)
        visited.add(name)

    visited = set()
    for name in graph:
        visit(name, set(), visited)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("Invalid JSON Schema") from None


def contract_hash(spec):
    payload = (
        spec.model_dump(mode="json", by_alias=True) if isinstance(spec, DefinitionSpec) else spec
    )
    if isinstance(payload, dict) and "allowed_operations" in payload:
        payload = {**payload, "allowed_operations": sorted(payload["allowed_operations"])}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _value_node(value, depth=0):
    if depth > 8:
        raise ValueError("Entry value depth exceeds the supported profile")
    if isinstance(value, dict):
        if len(value) > 32 or any(not isinstance(key, str) or len(key) > 64 for key in value):
            raise ValueError("Entry object is too large")
        for child in value.values():
            _value_node(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 1000:
            raise ValueError("Entry array is too large")
        for child in value:
            _value_node(child, depth + 1)
    elif isinstance(value, str) and len(value) > 16000:
        raise ValueError("Entry string is too long")


def validate_values(version, values, units=None):
    _value_node(values)
    try:
        encoded = json.dumps(
            values, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (RecursionError, TypeError, ValueError):
        raise ValueError("Entry values must be finite JSON") from None
    if len(encoded) > 65536:
        raise ValueError("Entry values exceed 64 KiB")
    try:
        Draft202012Validator(version.schema).validate(values)
    except ValidationError as error:
        path = ".".join(str(part) for part in error.absolute_path) or "payload"
        raise ValueError(f"Entry does not match definition at {path}") from None
    metadata = version.field_metadata
    for field, supplied in (units or {}).items():
        if field not in metadata or metadata[field].get("unit") != supplied:
            raise ValueError(f"Unit does not match definition for {field}")


def _system_payload_models():
    from garmin_ai.events import (
        ActivityEffort,
        Caffeine,
        ContextEvent,
        HeadacheObservation,
        Medication,
        Migraine,
        SymptomObservation,
        WellbeingObservation,
    )

    mapping = {
        "caffeine": Caffeine,
        "migraine": Migraine,
        "medication": Medication,
        "headache_observation": HeadacheObservation,
        "wellbeing_observation": WellbeingObservation,
        "activity_effort": ActivityEffort,
        "symptom_observation": SymptomObservation,
    }
    mapping.update({kind: ContextEvent for kind in SYSTEM_CONTEXT_KINDS})
    return mapping


def _system_topology(kind):
    if kind in {"migraine", "illness"}:
        return "open_interval"
    if kind in {"caffeine_absence", "caffeine_log_complete", "headache_observation"}:
        return "bounded_interval"
    if kind in {"wellbeing_observation", "activity_effort", "symptom_observation"}:
        return "point"
    return "flexible"


def _system_field_metadata(kind, name):
    if name in {"aura"}:
        return "boolean", "1"
    if name in {
        "severity",
        "perceived_exertion",
        "energy",
        "restedness",
        "pain",
        "functional_impact",
    }:
        return "ordinal", "score_1-10"
    if kind == "caffeine" and name.startswith("caffeine_mg_"):
        return "quantity", "mg"
    if kind == "caffeine" and name == "servings":
        return "count", "count"
    return "nominal", None


def _system_contract(kind, model):
    schema = model.model_json_schema()
    properties = schema.get("properties", {})
    fields = {}
    for name in properties:
        if name == "type":
            continue
        semantic, unit = _system_field_metadata(kind, name)
        fields[name] = {
            "id": f"system.{kind}.{name}",
            "labels": {"en": name.replace("_", " ")},
            "semantic": semantic,
            "unit": unit,
        }
    return {
        "key": f"system.{kind}",
        "labels": {"en": kind.replace("_", " ")},
        "schema": schema,
        "fields": fields,
        "topology": _system_topology(kind),
        "privacy": "sensitive",
        "allowed_operations": ["create", "update", "delete", "query"],
    }


def _version_for(session, definition):
    return session.scalar(
        select(EventDefinitionVersion).where(
            EventDefinitionVersion.definition_id == definition.id,
            EventDefinitionVersion.version == definition.current_version,
        )
    )


def ensure_system_definition(session, kind):
    model = _system_payload_models().get(kind)
    if model is None:
        raise ValueError("Unknown system event kind")
    key = f"system.{kind}"
    definition = session.scalar(select(EventDefinition).where(EventDefinition.key == key))
    contract = _system_contract(kind, model)
    digest = contract_hash(contract)
    if definition is None:
        definition = EventDefinition(
            namespace="system",
            key=key,
            status="active",
            revision=1,
            current_version=1,
        )
        session.add(definition)
        session.flush()
    current = _version_for(session, definition)
    if current is not None and current.schema_hash == digest:
        return current
    number = (
        session.scalar(
            select(func.max(EventDefinitionVersion.version)).where(
                EventDefinitionVersion.definition_id == definition.id
            )
        )
        or 0
    ) + 1
    version = EventDefinitionVersion(
        definition_id=definition.id,
        version=number,
        schema=contract["schema"],
        schema_hash=digest,
        topology=contract["topology"],
        field_metadata=contract["fields"],
        labels=contract["labels"],
        privacy=contract["privacy"],
        allowed_operations=contract["allowed_operations"],
    )
    session.add(version)
    session.flush()
    definition.current_version = number
    definition.revision += int(current is not None)
    session.flush()
    return version


def ensure_system_definitions(session, *, backfill=False):
    versions = {kind: ensure_system_definition(session, kind) for kind in _system_payload_models()}
    if backfill:
        for kind, version in versions.items():
            session.execute(
                update(Event)
                .where(Event.kind == kind, Event.definition_version_id.is_(None))
                .values(definition_version_id=version.id)
            )
    return versions


def _require_management(authorized):
    if not authorized:
        raise PermissionError("Definition management permission required")


def create_definition_draft(session, spec, *, actor, authorized=False):
    _require_management(authorized)
    spec = DefinitionSpec.model_validate(spec)
    if not KEY.fullmatch(spec.key) or session.scalar(
        select(EventDefinition.id).where(EventDefinition.key == spec.key)
    ):
        raise ValueError("Definition key is invalid or already exists")
    definition = EventDefinition(
        owner_id=owner(session).id,
        namespace="user",
        key=spec.key,
        status="draft",
        revision=1,
        draft={**spec.model_dump(mode="json", by_alias=True), "actor": actor},
    )
    session.add(definition)
    session.flush()
    return definition


def propose_definition_revision(session, definition_id, revision, spec, *, actor, authorized=False):
    _require_management(authorized)
    spec = DefinitionSpec.model_validate(spec)
    definition = session.scalar(
        select(EventDefinition).where(EventDefinition.id == definition_id).with_for_update()
    )
    if definition is None or definition.namespace != "user":
        raise LookupError("Definition not found")
    if definition.revision != revision:
        from garmin_ai.events import Conflict

        raise Conflict("Definition changed; reload before editing")
    if definition.status == "retired":
        raise ValueError("Retired definitions cannot be revised")
    if spec.key != definition.key:
        raise ValueError("Definition key is immutable")
    previous = _version_for(session, definition)
    if previous is not None:
        old_ids = {name: value["id"] for name, value in previous.field_metadata.items()}
        new_ids = {name: value.id for name, value in spec.fields.items()}
        if any(
            new_ids.get(name) != identity for name, identity in old_ids.items() if name in new_ids
        ):
            raise ValueError("Existing field identities are immutable")
    definition.draft = {**spec.model_dump(mode="json", by_alias=True), "actor": actor}
    if definition.current_version is None:
        definition.status = "proposed"
    definition.revision += 1
    session.flush()
    return definition


def activate_definition(session, definition_id, revision, *, actor, authorized=False):
    _require_management(authorized)
    definition = session.scalar(
        select(EventDefinition).where(EventDefinition.id == definition_id).with_for_update()
    )
    if definition is None or definition.namespace != "user":
        raise LookupError("Definition not found")
    if definition.revision != revision:
        from garmin_ai.events import Conflict

        raise Conflict("Definition changed; reload before activation")
    if definition.status not in {"draft", "proposed", "active"} or definition.draft is None:
        raise ValueError("Definition has no proposed contract")
    raw = {key: value for key, value in definition.draft.items() if key != "actor"}
    spec = DefinitionSpec.model_validate(raw)
    number = (
        session.scalar(
            select(func.max(EventDefinitionVersion.version)).where(
                EventDefinitionVersion.definition_id == definition.id
            )
        )
        or 0
    ) + 1
    version = EventDefinitionVersion(
        definition_id=definition.id,
        version=number,
        schema=spec.payload_schema,
        schema_hash=contract_hash(spec),
        topology=spec.topology,
        field_metadata={name: value.model_dump(mode="json") for name, value in spec.fields.items()},
        labels=spec.labels,
        privacy=spec.privacy,
        allowed_operations=sorted(spec.allowed_operations),
    )
    session.add(version)
    session.flush()
    definition.status = "active"
    definition.current_version = number
    definition.draft = None
    definition.revision += 1
    definition.updated_at = datetime.now(UTC)
    session.flush()
    return version


def retire_definition(session, definition_id, revision, *, authorized=False):
    _require_management(authorized)
    definition = session.scalar(
        select(EventDefinition).where(EventDefinition.id == definition_id).with_for_update()
    )
    if definition is None or definition.namespace != "user":
        raise LookupError("Definition not found")
    if definition.revision != revision:
        from garmin_ai.events import Conflict

        raise Conflict("Definition changed; reload before retirement")
    definition.status = "retired"
    definition.revision += 1
    session.flush()
    return definition


def active_version(session, key):
    definition = session.scalar(
        select(EventDefinition).where(
            EventDefinition.key == key,
            EventDefinition.namespace == "user",
            EventDefinition.status == "active",
        )
    )
    if definition is None:
        raise LookupError("Active definition not found")
    version = _version_for(session, definition)
    if version is None:
        raise LookupError("Active definition version not found")
    return definition, version


def _entry_values(entry, version):
    validate_values(version, entry.values, entry.units)
    start, end = entry.start, entry.end
    if version.topology == "point":
        if end not in {None, start}:
            raise ValueError("Point entry cannot have an interval")
        end, topology = None, "point"
    elif version.topology == "bounded_interval":
        if end is None or end <= start:
            raise ValueError("Bounded interval requires an end after its start")
        topology = "bounded_interval"
    elif version.topology == "open_interval":
        if end is not None and end <= start:
            if end < start:
                raise ValueError("Episode end must not precede its start")
            end, topology = None, "point"
        else:
            topology = "open_interval" if end is None else "bounded_interval"
    else:
        topology = "bounded_interval" if end is not None and end > start else "point"
        end = None if topology == "point" else end
    return {
        "definition_version_id": version.id,
        "kind": entry.definition_key,
        "start": start,
        "end": end,
        "timezone": entry.timezone,
        "source": entry.source,
        "confidence": entry.confidence,
        "status": entry.status,
        "original_text": entry.original_text,
        "payload": {"type": entry.definition_key, **entry.values},
        "topology": topology,
    }


def create_custom_event(session, entry, *, actor, idempotency_key=None, evidence_refs=None):
    from garmin_ai.events import (
        Conflict,
        invalidate_migraine_insights,
        lock_writes,
        replay_matches,
        serialize,
    )

    entry = CustomEntryInput.model_validate(entry)
    lock_writes(session)
    if idempotency_key is not None:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("Invalid idempotency key")
        existing = session.scalar(select(Event).where(Event.idempotency_key == idempotency_key))
        if existing is not None:
            version = session.get(EventDefinitionVersion, existing.definition_version_id)
            definition = session.get(EventDefinition, version.definition_id) if version else None
            if definition is None or definition.key != entry.definition_key:
                raise Conflict("Idempotency key already used for different data")
            return replay_matches(session, existing, _entry_values(entry, version))
    definition, version = active_version(session, entry.definition_key)
    if "create" not in version.allowed_operations:
        raise PermissionError("Definition does not allow creation")
    values = _entry_values(entry, version)
    from garmin_ai.canonical_events import provenance_values

    canonical = provenance_values(
        entry.source, entry.status, topology=values["topology"], actor=actor
    )
    statement = insert(Event).values(
        {
            **values,
            **canonical,
            "evidence_refs": evidence_refs or [],
            "idempotency_key": idempotency_key,
        }
    )
    if idempotency_key:
        statement = statement.on_conflict_do_nothing(index_elements=[Event.idempotency_key])
    event_id = session.scalar(statement.returning(Event.id))
    if event_id is None:
        existing = session.scalar(select(Event).where(Event.idempotency_key == idempotency_key))
        return replay_matches(session, existing, values)
    row = session.get(Event, event_id)
    invalidate_migraine_insights(session, row.kind)
    session.add(
        Audit(event_id=row.id, action="create", before=None, after=serialize(row), actor=actor)
    )
    from garmin_ai.metric_definitions import project_event_metrics

    project_event_metrics(session, row)
    return row


def update_custom_event(session, event_id: UUID, entry, *, revision, actor, evidence_refs=None):
    from garmin_ai.events import Conflict, lock_writes, serialize

    entry = CustomEntryInput.model_validate(entry)
    lock_writes(session)
    row = session.scalar(select(Event).where(Event.id == event_id).with_for_update())
    if row is None or row.deleted or row.definition_version_id is None:
        raise LookupError("Event not found")
    if row.revision != revision:
        raise Conflict("Event changed; reload before editing")
    version = session.get(EventDefinitionVersion, row.definition_version_id)
    definition = session.get(EventDefinition, version.definition_id) if version else None
    if (
        definition is None
        or definition.namespace != "user"
        or definition.key != entry.definition_key
    ):
        raise ValueError("Correction cannot change event definition")
    if "update" not in version.allowed_operations:
        raise PermissionError("Definition does not allow updates")
    before = serialize(row)
    for key, value in _entry_values(entry, version).items():
        setattr(row, key, value)
    from garmin_ai.canonical_events import provenance_values

    for key, value in provenance_values(
        entry.source, entry.status, topology=row.topology, actor=actor
    ).items():
        setattr(row, key, value)
    if evidence_refs is not None:
        row.evidence_refs = evidence_refs
    row.revision += 1
    session.flush()
    session.add(
        Audit(event_id=row.id, action="update", before=before, after=serialize(row), actor=actor)
    )
    from garmin_ai.metric_definitions import project_event_metrics

    project_event_metrics(session, row, rebuild=True)
    return row


def validate_stored_event(session, row):
    version = session.get(EventDefinitionVersion, row.definition_version_id)
    if version is None:
        raise ValueError("Event definition version is unavailable")
    values = {key: value for key, value in row.payload.items() if key != "type"}
    validate_values(version, values)
    return True


def list_definitions(session, *, include_retired=False):
    query = select(EventDefinition).order_by(EventDefinition.namespace, EventDefinition.key)
    if not include_retired:
        query = query.where(EventDefinition.status != "retired")
    return [
        {
            "id": str(row.id),
            "key": row.key,
            "namespace": row.namespace,
            "status": row.status,
            "revision": row.revision,
            "current_version": row.current_version,
        }
        for row in session.scalars(query)
    ]


def definition_state(row):
    return {
        "id": str(row.id),
        "key": row.key,
        "namespace": row.namespace,
        "status": row.status,
        "revision": row.revision,
        "current_version": row.current_version,
    }


def version_state(row):
    return {
        "id": str(row.id),
        "definition_id": str(row.definition_id),
        "version": row.version,
        "schema_hash": row.schema_hash,
        "topology": row.topology,
        "fields": row.field_metadata,
        "labels": row.labels,
        "privacy": row.privacy,
        "allowed_operations": row.allowed_operations,
    }
