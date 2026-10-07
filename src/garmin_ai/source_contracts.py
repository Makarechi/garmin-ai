"""Bounded source adapter contracts; persistence belongs to the application."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from garmin_ai.events import StrictModel


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
    payload: dict = Field(default_factory=dict, max_length=64)

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
