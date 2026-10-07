"""Fictional source and channel entry points, with no network access."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from garmin_ai.channels import ChannelCapabilities, InMemoryChannel
from garmin_ai.integrations import IntegrationFactory, PluginContext
from garmin_ai.source_contracts import SourceCapabilities, SourcePage, SourceRecord


class SampleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=50)


class SampleSource:
    def __init__(self, context: PluginContext):
        self.instance_id = context.instance_id
        self.label = context.config.label
        self.closed = False

    @property
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(observations=True, cursor=True, time_semantics="instant")

    def read_page(self, *, start, end, cursor, limit):
        if limit < 1 or limit > self.capabilities.max_page_size:
            raise ValueError("Unsupported page limit")
        observations = [
            ("walk-1", datetime(2026, 1, 1, 9, tzinfo=UTC), 15),
            ("walk-2", datetime(2026, 1, 1, 16, tzinfo=UTC), 25),
            ("walk-3", datetime(2026, 1, 1, 18, tzinfo=UTC), 10),
        ]
        selected = [row for row in observations if start <= row[1] < end]
        offset = int(cursor or "0")
        items = selected[offset : offset + limit]
        next_offset = offset + len(items)
        return SourcePage(
            instance_id=self.instance_id,
            # Each page is a fragment; finishing pagination does not authorize deletion.
            page_kind="partial",
            fetched_at=datetime(2026, 1, 2, tzinfo=UTC),
            next_cursor=str(next_offset) if next_offset < len(selected) else None,
            records=[
                SourceRecord(
                    source_record_id=key,
                    observed_at=at,
                    effective_at=at,
                    source_timezone="UTC",
                    source_reference=f"sample:{self.label}:{key}",
                    payload={"walk_minutes": minutes, "unit": "minutes"},
                )
                for key, at, minutes in items
            ],
        )

    def close(self):
        self.closed = True


class SampleChannel(InMemoryChannel):
    def __init__(self, context: PluginContext):
        super().__init__(ChannelCapabilities(text=True, max_text_length=200))
        self.instance_id = context.instance_id
        self.label = context.config.label


source_descriptor = IntegrationFactory(
    kind="source",
    provider="sample",
    plugin_factory=SampleSource,
    config_model=SampleConfig,
    capabilities=frozenset({"observations", "cursor"}),
    implementation_version="0.0.1",
)

channel_descriptor = IntegrationFactory(
    kind="channel",
    provider="sample",
    plugin_factory=SampleChannel,
    config_model=SampleConfig,
    capabilities=frozenset({"text"}),
    implementation_version="0.0.1",
)
