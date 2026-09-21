"""Channel-neutral tracker setup, actions and generated form handling."""

import hashlib
import json
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator
from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import delete, select

from garmin_ai.accounts import owner
from garmin_ai.definitions import (
    CustomEntryInput,
    DefinitionSpec,
    FieldSpec,
    activate_definition,
    create_custom_event,
    create_definition_draft,
    update_custom_event,
)
from garmin_ai.events import Conflict, StrictModel
from garmin_ai.models import AppState, Event, EventDefinition, EventDefinitionVersion, TrackerConfig


class TrackerFieldDraft(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    label: str = Field(min_length=1, max_length=120)
    kind: Literal["text", "number", "integer", "boolean", "choice", "scale"]
    required: bool = True
    unit: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_%./-]{1,32}$")
    minimum: float | None = None
    maximum: float | None = None
    options: list[str] = Field(default_factory=list, max_length=50)
    max_length: int = Field(default=500, ge=1, le=16000)

    @model_validator(mode="after")
    def valid_contract(self):
        numeric = self.kind in {"number", "integer", "scale"}
        if numeric:
            if (
                self.minimum is None
                or self.maximum is None
                or not math.isfinite(self.minimum)
                or not math.isfinite(self.maximum)
                or self.minimum > self.maximum
            ):
                raise ValueError("Numeric fields require finite bounds")
            if self.kind in {"integer", "scale"} and (
                not float(self.minimum).is_integer() or not float(self.maximum).is_integer()
            ):
                raise ValueError("Integer and scale bounds must be integers")
            if self.kind == "scale" and self.maximum - self.minimum > 20:
                raise ValueError("Scale range is too large")
        elif self.minimum is not None or self.maximum is not None:
            raise ValueError("Only numeric fields accept bounds")
        if self.kind == "choice":
            if not 1 <= len(self.options) <= 50 or len(self.options) != len(set(self.options)):
                raise ValueError("Choice fields require distinct options")
            if any(not value or len(value) > 120 for value in self.options):
                raise ValueError("Choice values must be nonempty and bounded")
        elif self.options:
            raise ValueError("Only choice fields accept options")
        if self.kind == "number" and not self.unit:
            raise ValueError("Number fields require a unit")
        if self.kind in {"text", "boolean", "choice"} and self.unit:
            raise ValueError("This field kind does not accept a unit")
        return self


class TrackerSetupDraft(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    name: str = Field(min_length=1, max_length=120)
    locale: str = Field(default="en", pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
    topology: Literal["point", "open_interval", "bounded_interval", "flexible"] = "point"
    fields: list[TrackerFieldDraft] = Field(min_length=1, max_length=32)
    shortcut: str | None = Field(default=None, max_length=64)
    reminder_enabled: bool = False
    reminder_time: str | None = Field(default=None, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    reminder_timezone: str = "UTC"
    privacy: Literal["private", "sensitive"] = "private"

    @model_validator(mode="after")
    def valid_setup(self):
        if len({field.key for field in self.fields}) != len(self.fields):
            raise ValueError("Tracker field keys must be distinct")
        if self.reminder_enabled and self.reminder_time is None:
            raise ValueError("Enabled reminder requires a time")
        try:
            ZoneInfo(self.reminder_timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown reminder timezone") from None
        return self


class TrackerConfirmation(StrictModel):
    draft: TrackerSetupDraft
    confirmation_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class ActionSpec(StrictModel):
    id: str
    kind: Literal["create_entry", "edit_entry"]
    label: str
    definition_key: str
    definition_version_id: UUID
    form_id: str
    event_id: UUID | None = None
    revision: int | None = None


class FormFieldSpec(StrictModel):
    name: str
    field_id: str
    label: str
    input: Literal["text", "number", "integer", "boolean", "choice", "json"]
    required: bool
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None
    options: list = Field(default_factory=list)


class FormSpec(StrictModel):
    id: str
    action: ActionSpec
    title: str
    topology: str
    schema_hash: str
    fields: list[FormFieldSpec]
    initial_values: dict = Field(default_factory=dict)
    initial_units: dict[str, str] = Field(default_factory=dict)
    initial_start: AwareDatetime | None = None
    initial_end: AwareDatetime | None = None
    initial_timezone: str | None = None


class FormSubmission(StrictModel):
    action_id: str
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: AwareDatetime
    end: AwareDatetime | None = None
    timezone: str = "UTC"
    values: dict = Field(default_factory=dict, max_length=32)
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


class FormValidationError(ValueError):
    def __init__(self, errors):
        super().__init__("Form validation failed")
        self.errors = errors


def _field_schema(field: TrackerFieldDraft):
    if field.kind == "text":
        return {
            "type": "string",
            "minLength": 1 if field.required else 0,
            "maxLength": field.max_length,
        }
    if field.kind == "choice":
        return {"type": "string", "enum": field.options}
    if field.kind == "boolean":
        return {"type": "boolean"}
    return {
        "type": "number" if field.kind == "number" else "integer",
        "minimum": field.minimum,
        "maximum": field.maximum,
    }


def definition_spec(draft: TrackerSetupDraft):
    properties = {field.key: _field_schema(field) for field in draft.fields}
    fields = {}
    for field in draft.fields:
        semantic = {
            "text": "text",
            "choice": "nominal",
            "boolean": "boolean",
            "number": "quantity",
            "integer": "count",
            "scale": "ordinal",
        }[field.kind]
        unit = field.unit
        if field.kind == "integer" and unit is None:
            unit = "count"
        if field.kind == "scale":
            unit = f"score_{int(field.minimum)}-{int(field.maximum)}"
        fields[field.key] = FieldSpec(
            id=f"user.{draft.key}.{field.key}",
            labels={draft.locale: field.label},
            semantic=semantic,
            unit=unit,
        )
    return DefinitionSpec(
        key=f"user.{draft.key}",
        labels={draft.locale: draft.name},
        schema={
            "type": "object",
            "properties": properties,
            "required": [field.key for field in draft.fields if field.required],
            "additionalProperties": False,
        },
        fields=fields,
        topology=draft.topology,
        privacy=draft.privacy,
    )


def _draft_hash(draft):
    encoded = json.dumps(
        draft.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _label(labels, locale):
    return (
        labels.get(locale)
        or labels.get(locale.split("-", 1)[0])
        or labels.get("en")
        or next(iter(labels.values()))
    )


def _form_fields(schema, metadata, locale):
    required = set(schema.get("required", []))
    fields = []
    for name, node in schema.get("properties", {}).items():
        field = metadata[name]
        kind = node.get("type")
        input_kind = (
            "choice"
            if "enum" in node
            else "integer"
            if kind == "integer"
            else "number"
            if kind == "number"
            else "boolean"
            if kind == "boolean"
            else "text"
            if kind == "string"
            else "json"
        )
        fields.append(
            FormFieldSpec(
                name=name,
                field_id=field["id"],
                label=_label(field["labels"], locale),
                input=input_kind,
                required=name in required,
                unit=field.get("unit"),
                minimum=node.get("minimum", node.get("exclusiveMinimum")),
                maximum=node.get("maximum", node.get("exclusiveMaximum")),
                max_length=node.get("maxLength"),
                options=node.get("enum", []),
            )
        )
    return fields


def preview_tracker(session, draft: TrackerSetupDraft):
    draft = TrackerSetupDraft.model_validate(draft)
    spec = definition_spec(draft)
    token = secrets.token_hex(32)
    now = datetime.now(UTC)
    session.execute(
        delete(AppState).where(
            AppState.key.startswith("tracker-preview:"),
            AppState.value["expires_at"].as_string() < now.isoformat(),
        )
    )
    session.add(
        AppState(
            key="tracker-preview:" + token,
            value={
                "draft_hash": _draft_hash(draft),
                "expires_at": (now + timedelta(minutes=15)).isoformat(),
            },
        )
    )
    session.flush()
    return {
        "confirmation_token": token,
        "definition": spec.model_dump(mode="json", by_alias=True),
        "form": {
            "title": draft.name,
            "topology": draft.topology,
            "fields": [
                field.model_dump(mode="json")
                for field in _form_fields(
                    spec.payload_schema,
                    {name: value.model_dump(mode="json") for name, value in spec.fields.items()},
                    draft.locale,
                )
            ],
        },
        "settings": {
            "shortcut": draft.shortcut,
            "reminder_enabled": draft.reminder_enabled,
            "reminder_time": draft.reminder_time,
            "reminder_timezone": draft.reminder_timezone,
        },
    }


def _action(definition, version, tracker, locale, *, event=None):
    if event is None:
        identity = f"create:{version.id}"
        kind = "create_entry"
        revision = None
    else:
        identity = f"edit:{event.id}:{event.revision}"
        kind = "edit_entry"
        revision = event.revision
    label = tracker.shortcut if tracker and tracker.shortcut else _label(version.labels, locale)
    return ActionSpec(
        id=identity,
        kind=kind,
        label=label,
        definition_key=definition.key,
        definition_version_id=version.id,
        form_id=identity,
        event_id=event.id if event else None,
        revision=revision,
    )


def available_actions(session, *, locale="en"):
    person = owner(session)
    rows = session.execute(
        select(EventDefinition, EventDefinitionVersion, TrackerConfig)
        .join(
            EventDefinitionVersion,
            (EventDefinitionVersion.definition_id == EventDefinition.id)
            & (EventDefinitionVersion.version == EventDefinition.current_version),
        )
        .outerjoin(TrackerConfig, TrackerConfig.definition_id == EventDefinition.id)
        .where(
            EventDefinition.owner_id == person.id,
            EventDefinition.namespace == "user",
            EventDefinition.status == "active",
        )
        .order_by(EventDefinition.key)
    ).all()
    return [
        _action(definition, version, tracker, locale)
        for definition, version, tracker in rows
        if "create" in version.allowed_operations
    ]


def action_for_event(session, event_id, *, locale="en"):
    event = session.get(Event, event_id)
    if event is None or event.deleted or event.definition_version_id is None:
        raise LookupError("Event not found")
    version = session.get(EventDefinitionVersion, event.definition_version_id)
    definition = session.get(EventDefinition, version.definition_id) if version else None
    if (
        definition is None
        or definition.namespace != "user"
        or "update" not in version.allowed_operations
        or "query" not in version.allowed_operations
    ):
        raise LookupError("Editable tracker entry not found")
    tracker = session.scalar(
        select(TrackerConfig).where(TrackerConfig.definition_id == definition.id)
    )
    return _action(definition, version, tracker, locale, event=event)


def _resolve_action(session, action_id):
    parts = action_id.split(":")
    if len(parts) == 2 and parts[0] == "create":
        try:
            version_id = UUID(parts[1])
        except ValueError:
            raise LookupError("Form action not found") from None
        version = session.get(EventDefinitionVersion, version_id)
        definition = session.get(EventDefinition, version.definition_id) if version else None
        if (
            definition is None
            or definition.namespace != "user"
            or definition.status != "active"
            or definition.current_version != version.version
            or "create" not in version.allowed_operations
        ):
            raise Conflict("Form contract is no longer active; reload it")
        return definition, version, None
    if len(parts) == 3 and parts[0] == "edit":
        try:
            event_id, revision = UUID(parts[1]), int(parts[2])
        except ValueError:
            raise LookupError("Form action not found") from None
        event = session.get(Event, event_id)
        if event is None or event.deleted:
            raise LookupError("Event not found")
        version = session.get(EventDefinitionVersion, event.definition_version_id)
        definition = session.get(EventDefinition, version.definition_id) if version else None
        if (
            definition is None
            or definition.namespace != "user"
            or "update" not in version.allowed_operations
            or "query" not in version.allowed_operations
        ):
            raise LookupError("Editable tracker entry not found")
        if event.revision != revision:
            raise Conflict("Entry changed; reload its form")
        return definition, version, event
    raise LookupError("Form action not found")


def form_for_action(session, action_id, *, locale="en"):
    definition, version, event = _resolve_action(session, action_id)
    tracker = session.scalar(
        select(TrackerConfig).where(TrackerConfig.definition_id == definition.id)
    )
    action = _action(definition, version, tracker, locale, event=event)
    return FormSpec(
        id=action.id,
        action=action,
        title=_label(version.labels, locale),
        topology=version.topology,
        schema_hash=version.schema_hash,
        fields=_form_fields(version.schema, version.field_metadata, locale),
        initial_values=(
            {key: value for key, value in event.payload.items() if key != "type"} if event else {}
        ),
        initial_units=(
            {
                name: metadata["unit"]
                for name, metadata in version.field_metadata.items()
                if metadata.get("unit") and name in event.payload
            }
            if event
            else {}
        ),
        initial_start=event.start if event else None,
        initial_end=event.end if event else None,
        initial_timezone=event.timezone if event else None,
    )


def _validation_errors(version, submission):
    errors = []
    for error in Draft202012Validator(version.schema).iter_errors(submission.values):
        field = str(next(iter(error.absolute_path), "payload"))
        if error.validator == "required":
            missing = sorted(set(error.validator_value) - set(error.instance))
            errors.extend(
                {"field": name, "code": "required", "message": "This field is required"}
                for name in missing
            )
        elif error.validator == "additionalProperties":
            errors.append({"field": "payload", "code": "unknown_field", "message": "Unknown field"})
        else:
            errors.append(
                {"field": field, "code": str(error.validator), "message": "Invalid field value"}
            )
    for name, unit in submission.units.items():
        expected = version.field_metadata.get(name, {}).get("unit")
        if expected != unit:
            errors.append(
                {"field": name, "code": "unit", "message": "Unit does not match the form"}
            )
    if version.topology == "point" and submission.end not in {None, submission.start}:
        errors.append({"field": "end", "code": "topology", "message": "End is not used"})
    if version.topology == "bounded_interval" and (
        submission.end is None or submission.end <= submission.start
    ):
        errors.append({"field": "end", "code": "topology", "message": "A later end is required"})
    if (
        version.topology == "open_interval"
        and submission.end is not None
        and submission.end < submission.start
    ):
        errors.append({"field": "end", "code": "topology", "message": "End cannot precede start"})
    return errors


def submit_form(
    session,
    action_id,
    submission,
    *,
    actor,
    source="manual",
    idempotency_key=None,
    original_text=None,
    evidence_refs=None,
):
    submission = FormSubmission.model_validate(submission)
    if submission.action_id != action_id:
        raise Conflict("Form action does not match the route")
    definition, version, event = _resolve_action(session, action_id)
    if submission.schema_hash != version.schema_hash:
        raise Conflict("Form contract changed; reload it")
    errors = _validation_errors(version, submission)
    if errors:
        raise FormValidationError(errors)
    entry = CustomEntryInput(
        definition_key=definition.key,
        start=submission.start,
        end=submission.end,
        timezone=submission.timezone,
        source=source,
        original_text=original_text,
        values=submission.values,
        units=submission.units,
    )
    if event is None:
        return create_custom_event(
            session,
            entry,
            actor=actor,
            idempotency_key=idempotency_key,
            evidence_refs=evidence_refs,
        )
    return update_custom_event(
        session,
        event.id,
        entry,
        revision=event.revision,
        actor=actor,
        evidence_refs=evidence_refs,
    )


def confirm_tracker(session, confirmation, *, actor):
    confirmation = TrackerConfirmation.model_validate(confirmation)
    draft = confirmation.draft
    preview = session.get(AppState, "tracker-preview:" + confirmation.confirmation_token)
    if (
        preview is None
        or datetime.fromisoformat(preview.value["expires_at"]) <= datetime.now(UTC)
        or preview.value.get("draft_hash") != _draft_hash(draft)
    ):
        raise Conflict("Tracker preview changed; preview it again")
    session.delete(preview)
    definition = create_definition_draft(
        session, definition_spec(draft), actor=actor, authorized=True
    )
    version = activate_definition(
        session, definition.id, definition.revision, actor=actor, authorized=True
    )
    tracker = TrackerConfig(
        owner_id=owner(session).id,
        definition_id=definition.id,
        shortcut=draft.shortcut or draft.name,
        reminder_enabled=draft.reminder_enabled,
        reminder_time=draft.reminder_time,
        reminder_timezone=draft.reminder_timezone,
    )
    session.add(tracker)
    session.flush()
    return {
        "tracker": {
            "id": str(tracker.id),
            "definition_id": str(definition.id),
            "definition_key": definition.key,
            "revision": tracker.revision,
            "shortcut": tracker.shortcut,
            "reminder_enabled": tracker.reminder_enabled,
            "reminder_time": tracker.reminder_time,
            "reminder_timezone": tracker.reminder_timezone,
        },
        "action": _action(definition, version, tracker, draft.locale).model_dump(mode="json"),
    }
