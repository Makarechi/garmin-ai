"""Bounded local CSV/JSON source with explicit mapping and replay-safe import."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import islice
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, model_validator

from garmin_ai.accounts import owner
from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.models import AppState, SourcePayload
from garmin_ai.source_contracts import (
    SourceCapabilities,
    SourcePage,
    SourceRecord,
    record_overlaps_window,
)
from garmin_ai.tracker_forms import FormSubmission, _validation_errors, form_for_action, submit_form

MAX_FILE_BYTES = 512_000
MAX_ROWS = 500
ROW_CODES = frozenset(
    {
        "source_id_required",
        "duplicate_source_id",
        "timestamp_type",
        "timestamp_format",
        "timestamp_offset_required",
        "timestamp_timezone_mismatch",
        "end_required",
        "complex_field_unsupported",
        "boolean_format",
        "numeric_format",
        "decimal_separator",
        "integer_format",
        "text_format",
    }
)


class FileMapping(StrictModel):
    format: Literal["csv", "json"]
    source_instance_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    device_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    definition_key: str = Field(pattern=r"^user\.[a-z][a-z0-9_]{0,62}$")
    row_id_column: str = Field(min_length=1, max_length=100)
    start_column: str = Field(min_length=1, max_length=100)
    end_column: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=100)
    field_columns: dict[str, str] = Field(min_length=1, max_length=32)
    units: dict[str, str] = Field(default_factory=dict, max_length=32)
    decimal_separator: Literal[".", ","] = "."
    null_markers: list[str] = Field(default_factory=lambda: [""], max_length=16)

    @model_validator(mode="after")
    def valid_mapping(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Mapping timezone must be an IANA name") from None
        if any(not item or len(item) > 100 for item in self.field_columns.values()):
            raise ValueError("Field columns must be named")
        if len(set(self.null_markers)) != len(self.null_markers):
            raise ValueError("Null markers must be distinct")
        return self


@dataclass
class ImportPlan:
    mapping: FileMapping
    file_hash: str
    plan_hash: str
    form: object
    rows: list[dict]
    records: list[SourceRecord]
    issues: list[dict]


def _digest(data) -> str:
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value):
    raise ValueError("Non-finite JSON value")


def _read_rows(content: str, format: str) -> list[dict]:
    try:
        if format == "json":
            rows = json.loads(
                content, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            if not isinstance(rows, list):
                raise ValueError("JSON root must be a list")
        else:
            reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError("CSV headers must be unique")
            rows = list(islice(reader, MAX_ROWS + 1))
            if any(None in row for row in rows):
                raise ValueError("CSV row has more fields than headers")
        if not 1 <= len(rows) <= MAX_ROWS or any(not isinstance(row, dict) for row in rows):
            raise ValueError("File must contain 1 to 500 object rows")
        try:
            json.dumps(rows, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError("File contains a non-finite or unsupported value") from None
        return rows
    except (csv.Error, UnicodeError, json.JSONDecodeError, OverflowError, RecursionError):
        raise ValueError("Invalid CSV or JSON file") from None


def _time(value, timezone: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp_type")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("timestamp_format") from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp_offset_required")
    if stamp.utcoffset() != stamp.astimezone(ZoneInfo(timezone)).utcoffset():
        raise ValueError("timestamp_timezone_mismatch")
    return stamp


def _value(raw, field, mapping: FileMapping):
    if raw is None or (isinstance(raw, str) and raw in mapping.null_markers):
        return None
    kind = field.input
    if kind == "json":
        raise ValueError("complex_field_unsupported")
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw
        if raw in {"true", "false"}:
            return raw == "true"
        raise ValueError("boolean_format")
    if kind in {"integer", "number"}:
        if isinstance(raw, bool):
            raise ValueError("numeric_format")
        if not isinstance(raw, int | float | str):
            raise ValueError("numeric_format")
        value = str(raw)
        if isinstance(raw, str):
            if mapping.decimal_separator == ",":
                if "." in value:
                    raise ValueError("decimal_separator")
                value = value.replace(",", ".")
            elif "," in value:
                raise ValueError("decimal_separator")
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise ValueError("numeric_format") from None
        if not parsed.is_finite():
            raise ValueError("numeric_format")
        if kind == "integer":
            if parsed != parsed.to_integral_value():
                raise ValueError("integer_format")
            return int(parsed)
        number = float(parsed)
        if not math.isfinite(number):
            raise ValueError("numeric_format")
        return number
    if not isinstance(raw, str):
        raise ValueError("text_format")
    return raw


def _action_id(session, key: str) -> str:
    from garmin_ai.definitions import active_version

    _, version = active_version(session, key)
    return f"create:{version.id}"


def build_plan(session, file_path: Path, mapping_path: Path) -> ImportPlan:
    try:
        if file_path.stat().st_size > MAX_FILE_BYTES or mapping_path.stat().st_size > 16_000:
            raise ValueError("File or mapping exceeds the local size limit")
        data = file_path.read_bytes()
        mapping_data = mapping_path.read_bytes()
        if len(data) > MAX_FILE_BYTES or len(mapping_data) > 16_000:
            raise ValueError("File or mapping exceeds the local size limit")
        content = data.decode("utf-8-sig")
        try:
            mapping = FileMapping.model_validate_json(mapping_data)
        except ValidationError:
            raise ValueError("Invalid mapping file") from None
    except UnicodeError:
        raise ValueError("File must use UTF-8") from None
    rows = _read_rows(content, mapping.format)
    form = form_for_action(session, _action_id(session, mapping.definition_key))
    if form.complex_schema or any(field.input == "json" for field in form.fields):
        raise ValueError("Complex tracker forms are not supported by file import")
    fields = {field.name: field for field in form.fields}
    if set(mapping.field_columns) - set(fields):
        raise ValueError("Mapping names a field outside the active tracker")
    expected_units = {
        name: fields[name].unit for name in mapping.field_columns if fields[name].unit
    }
    if mapping.units != expected_units:
        raise ValueError("Mapping must specify every field's exact unit")
    file_hash = hashlib.sha256(data).hexdigest()
    plan_hash = _digest(
        {
            "file_hash": file_hash,
            "mapping": mapping.model_dump(mode="json"),
            "schema_hash": form.schema_hash,
        }
    )
    issues = []
    records = []
    seen_ids = set()
    for index, row in enumerate(rows, 1):
        try:
            identity = row.get(mapping.row_id_column)
            if not isinstance(identity, str) or not 1 <= len(identity) <= 200:
                raise ValueError("source_id_required")
            if identity in seen_ids:
                raise ValueError("duplicate_source_id")
            seen_ids.add(identity)
            start = _time(row.get(mapping.start_column), mapping.timezone)
            end = None
            if mapping.end_column:
                raw_end = row.get(mapping.end_column)
                if raw_end is None or raw_end == "":
                    raise ValueError("end_required")
                end = _time(raw_end, mapping.timezone)
            values = {}
            for name, column in mapping.field_columns.items():
                if column not in row:
                    raise ValueError(f"missing_column:{name}")
                converted = _value(row[column], fields[name], mapping)
                if converted is not None:
                    values[name] = converted
            submission = FormSubmission(
                action_id=form.id,
                schema_hash=form.schema_hash,
                start=start,
                end=end,
                timezone=mapping.timezone,
                values=values,
                units={name: unit for name, unit in mapping.units.items() if name in values},
            )
            from garmin_ai.definitions import active_version

            _, version = active_version(session, mapping.definition_key)
            errors = _validation_errors(version, submission)
            if errors:
                issue = errors[0]
                raise ValueError(f"form:{issue['field']}:{issue['code']}")
            records.append(
                SourceRecord(
                    source_record_id=identity,
                    observed_at=start,
                    effective_at=start,
                    effective_end=end,
                    source_timezone=mapping.timezone,
                    source_reference=f"sha256:{file_hash}:{_digest(identity)}",
                    payload={
                        "values": values,
                        "units": submission.units,
                        "end": end.isoformat() if end else None,
                    },
                )
            )
        except (ValueError, TypeError) as exc:
            parts = str(exc).split(":", 2)
            if parts[0] == "form" and len(parts) == 3:
                issues.append({"row": index, "field": parts[1], "code": parts[2]})
            elif parts[0] == "missing_column" and len(parts) >= 2:
                issues.append({"row": index, "field": parts[1], "code": "missing_column"})
            else:
                issues.append(
                    {"row": index, "code": parts[0] if parts[0] in ROW_CODES else "invalid_row"}
                )
    return ImportPlan(mapping, file_hash, plan_hash, form, rows, records, issues)


class FileSourceAdapter:
    def __init__(self, plan: ImportPlan):
        self.plan = plan
        self.instance_id = f"{plan.mapping.source_instance_id}:{plan.mapping.device_id}"

    @property
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            observations=True,
            cursor=True,
            time_semantics="interval" if self.plan.mapping.end_column else "instant",
        )

    def read_page(
        self, *, start: datetime, end: datetime, cursor: str | None, limit: int
    ) -> SourcePage:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid source page size")
        index = (
            int(cursor)
            if cursor is not None and cursor.isdecimal()
            else 0
            if cursor is None
            else -1
        )
        if index < 0 or index > len(self.plan.records):
            raise ValueError("Invalid source cursor")
        time_semantics = self.capabilities.time_semantics
        records = []
        next_index = index
        while next_index < len(self.plan.records) and len(records) < limit:
            record = self.plan.records[next_index]
            next_index += 1
            if record_overlaps_window(record, time_semantics, start, end):
                records.append(record)
        return SourcePage(
            instance_id=self.instance_id,
            page_kind="partial",
            fetched_at=datetime.now(UTC),
            next_cursor=str(next_index) if next_index < len(self.plan.records) else None,
            records=records,
        )

    def close(self) -> None:
        pass


def preview(plan: ImportPlan) -> dict:
    fields = {field.name: field for field in plan.form.fields}
    return {
        "file_sha256": plan.file_hash,
        "plan_sha256": plan.plan_hash,
        "definition_key": plan.mapping.definition_key,
        "source_instance_id": plan.mapping.source_instance_id,
        "device_id": plan.mapping.device_id,
        "definition_version_id": str(plan.form.action.definition_version_id),
        "mapping": {
            "format": plan.mapping.format,
            "row_id_column": plan.mapping.row_id_column,
            "start_column": plan.mapping.start_column,
            "end_column": plan.mapping.end_column,
            "timezone": plan.mapping.timezone,
            "decimal_separator": plan.mapping.decimal_separator,
            "null_marker_count": len(plan.mapping.null_markers),
            "fields": [
                {
                    "name": name,
                    "column": column,
                    "input": fields[name].input,
                    "unit": fields[name].unit,
                    "minimum": fields[name].minimum,
                    "maximum": fields[name].maximum,
                }
                for name, column in plan.mapping.field_columns.items()
            ],
        },
        "row_count": len(plan.rows),
        "valid_count": len(plan.records),
        "error_count": len(plan.issues),
        "errors": plan.issues[:20],
    }


def apply(session, plan: ImportPlan, confirmation: str) -> dict:
    if plan.issues:
        raise ValueError("Fix preview errors before importing")
    if confirmation != plan.plan_hash:
        raise Conflict("File, mapping or tracker contract changed; preview again")
    lock_writes(session)
    person = owner(session)
    adapter = FileSourceAdapter(plan)
    contract_hash = _digest(
        {
            "mapping": plan.mapping.model_dump(mode="json"),
            "schema_hash": plan.form.schema_hash,
        }
    )
    raw_by_id = {
        record.source_record_id: row for record, row in zip(plan.records, plan.rows, strict=True)
    }
    created = skipped = 0
    cursor = None
    while True:
        page = adapter.read_page(
            start=datetime.min.replace(tzinfo=UTC),
            end=datetime.max.replace(tzinfo=UTC),
            cursor=cursor,
            limit=100,
        )
        for record in page.records:
            raw = raw_by_id[record.source_record_id]
            identity = _digest(
                [
                    str(person.id),
                    plan.mapping.source_instance_id,
                    plan.mapping.device_id,
                    record.source_record_id,
                ]
            )
            state_key = f"file-import:row:{identity}"
            row_hash = _digest(raw)
            state = session.get(AppState, state_key)
            if state is not None:
                if (
                    state.value.get("row_hash") != row_hash
                    or state.value.get("contract_hash") != contract_hash
                ):
                    raise Conflict(
                        "Source row or mapping changed; preserve the edited diary entry and resolve separately"
                    )
                skipped += 1
                continue
            payload = SourcePayload(
                source=f"file_import:{plan.mapping.source_instance_id}:{plan.mapping.device_id}",
                endpoint=plan.mapping.definition_key,
                source_key=identity,
                payload_hash=row_hash,
                payload=raw,
                archive_key=f"sha256:{plan.file_hash}",
                fetched_at=page.fetched_at,
                status="applied",
            )
            session.add(payload)
            session.flush()
            values = record.payload["values"]
            units = record.payload["units"]
            event = submit_form(
                session,
                plan.form.id,
                FormSubmission(
                    action_id=plan.form.id,
                    schema_hash=plan.form.schema_hash,
                    start=record.effective_at,
                    end=record.effective_end,
                    timezone=plan.mapping.timezone,
                    values=values,
                    units=units,
                ),
                actor=f"file_import:{plan.mapping.source_instance_id}",
                source="file_import",
                idempotency_key=f"file-import:{identity}",
                evidence_refs=[
                    {"source_payload_id": str(payload.id), "file_sha256": plan.file_hash}
                ],
            )
            session.add(
                AppState(
                    key=state_key,
                    value={
                        "row_hash": row_hash,
                        "contract_hash": contract_hash,
                        "event_id": str(event.id),
                    },
                )
            )
            created += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    session.merge(
        AppState(
            key=f"file-import:cursor:{plan.plan_hash}",
            value={"complete": True, "rows": len(plan.rows)},
        )
    )
    return {"created": created, "skipped": skipped, "file_sha256": plan.file_hash}
