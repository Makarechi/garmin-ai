"""Bounded source adapter contracts; persistence belongs to the application."""

from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from garmin_ai.events import StrictModel

MAX_SOURCE_PAYLOAD_BYTES = 16_384
MAX_SOURCE_PAYLOAD_DEPTH = 8
MAX_SOURCE_COLLECTION_ITEMS = 256


def _validate_payload_tree(value, depth: int = 0) -> None:
    if depth > MAX_SOURCE_PAYLOAD_DEPTH:
        raise ValueError("Source payload exceeds the nesting limit")
    if isinstance(value, dict):
        if len(value) > MAX_SOURCE_COLLECTION_ITEMS or any(
            not isinstance(key, str) for key in value
        ):
            raise ValueError("Source payload must use bounded JSON objects")
        for item in value.values():
            _validate_payload_tree(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_SOURCE_COLLECTION_ITEMS:
            raise ValueError("Source payload exceeds the collection limit")
        for item in value:
            _validate_payload_tree(item, depth + 1)
    elif value is None or isinstance(value, str | bool | int):
        return
    elif isinstance(value, float) and math.isfinite(value):
        return
    else:
        raise ValueError("Source payload must contain finite JSON values")


class SourceCapabilities(StrictModel):
    observations: bool = True
    corrections: bool = False
    deletions: bool = False
    cursor: bool = False
    time_semantics: Literal["instant", "interval", "calendar_day"] = "instant"
    max_page_size: int = Field(default=100, ge=1, le=1000)


class SourceRecord(StrictModel):
    source_record_id: str = Field(min_length=1, max_length=200)
    operation: Literal["upsert", "delete"] = "upsert"
    observed_at: AwareDatetime
    effective_at: AwareDatetime
    effective_end: AwareDatetime | None = None
    source_timezone: str = Field(min_length=1, max_length=100)
    source_reference: str = Field(min_length=1, max_length=500)
    payload: dict = Field(default_factory=dict, max_length=MAX_SOURCE_COLLECTION_ITEMS)

    @model_validator(mode="after")
    def deletion_has_no_payload(self):
        if self.operation == "delete" and self.payload:
            raise ValueError("Deletion records cannot carry an observation payload")
        if self.effective_end is not None and self.effective_end <= self.effective_at:
            raise ValueError("Effective end must be after effective start")
        try:
            ZoneInfo(self.source_timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Source timezone must be an IANA name") from None
        _validate_payload_tree(self.payload)
        try:
            size = len(json.dumps(self.payload, ensure_ascii=False, allow_nan=False).encode())
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
            raise ValueError("Source payload must contain finite JSON values") from None
        if size > MAX_SOURCE_PAYLOAD_BYTES:
            raise ValueError("Source payload exceeds the byte limit")
        return self


class SourcePage(StrictModel):
    instance_id: str = Field(min_length=1, max_length=200)
    page_kind: Literal["partial", "complete_interval_snapshot"]
    fetched_at: AwareDatetime
    next_cursor: str | None = Field(default=None, min_length=1, max_length=1000)
    records: list[SourceRecord] = Field(default_factory=list, max_length=1000)
    retry_after: AwareDatetime | None = None

    @model_validator(mode="after")
    def unique_record_ids(self):
        if len({item.source_record_id for item in self.records}) != len(self.records):
            raise ValueError("A source page cannot repeat a record identity")
        if self.page_kind == "complete_interval_snapshot" and self.next_cursor is not None:
            raise ValueError("Complete interval snapshots cannot have a next cursor")
        return self


def record_overlaps_window(
    record: SourceRecord,
    time_semantics: Literal["instant", "interval", "calendar_day"],
    start: datetime,
    end: datetime,
) -> bool:
    """Match a record to an absolute window using its declared time semantics."""

    if time_semantics == "interval":
        return (
            record.effective_end is not None
            and record.effective_at < end
            and record.effective_end > start
        )
    if time_semantics == "calendar_day":
        zone = ZoneInfo(record.source_timezone)
        local_day = record.effective_at.astimezone(zone).date()
        local_start = datetime.combine(local_day, time.min, tzinfo=zone)
        local_end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=zone)
        return local_start < end and local_end > start
    return start <= record.effective_at < end


class SourceAdapter(Protocol):
    @property
    def capabilities(self) -> SourceCapabilities: ...

    def read_page(
        self,
        *,
        start: datetime,
        end: datetime,
        cursor: str | None,
        limit: int,
    ) -> SourcePage: ...

    def close(self) -> None: ...
