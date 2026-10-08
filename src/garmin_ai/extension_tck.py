"""Reusable local contract checks for trusted external integration packages."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from garmin_ai.channels import (
    ChannelCapabilities,
    ChannelInstanceRef,
    DeliveryAttempt,
    DeliveryPolicy,
    DeliveryState,
    OutboundIntent,
    TextBlock,
)
from garmin_ai.source_contracts import SourceCapabilities, SourcePage, record_overlaps_window


class ModelProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urgent: bool


def check_source_adapter(adapter, *, instance_id: str, max_pages: int = 1000) -> dict:
    """Probe a bounded synthetic window; never call a live account in CI."""

    capabilities = adapter.capabilities
    if not isinstance(capabilities, SourceCapabilities) or not capabilities.observations:
        raise AssertionError("Source must declare observation capability")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    now = datetime(2026, 1, 2, tzinfo=UTC)
    start = now - timedelta(days=1)
    limit = min(2, capabilities.max_page_size)
    seen = set()
    cursors = set()
    cursor = None
    pages = 0
    while True:
        page = SourcePage.model_validate(
            adapter.read_page(start=start, end=now, cursor=cursor, limit=limit)
        )
        if page.instance_id != instance_id or len(page.records) > limit:
            raise AssertionError("Source returned a different instance or exceeded the page limit")
        if capabilities.time_semantics == "interval":
            if any(row.effective_end is None for row in page.records):
                raise AssertionError("Interval source records require an effective end")
        if any(
            not record_overlaps_window(row, capabilities.time_semantics, start, now)
            for row in page.records
        ):
            raise AssertionError("Source returned records outside the requested window")
        if any(row.operation == "delete" for row in page.records) and not capabilities.deletions:
            raise AssertionError("Source returned undeclared deletions")
        identities = {(page.instance_id, row.source_record_id) for row in page.records}
        if seen & identities:
            raise AssertionError("Source repeated an identity across pages")
        seen |= identities
        pages += 1
        if page.next_cursor is None:
            break
        if not capabilities.cursor or page.next_cursor == cursor or page.next_cursor in cursors:
            raise AssertionError("Source cursor repeated or was undeclared")
        if pages >= max_pages:
            raise AssertionError("Source exceeded the configured page budget")
        cursors.add(page.next_cursor)
        cursor = page.next_cursor
    return {"pages": pages, "records": len(seen), "instance_id": instance_id}


async def check_channel_adapter(
    adapter,
    *,
    instance_id: str,
    owner_id: UUID | None = None,
    conversation_id: UUID | None = None,
) -> dict:
    capabilities = adapter.capabilities
    if not isinstance(capabilities, ChannelCapabilities) or not capabilities.text:
        raise AssertionError("Channel must declare text capability")
    parts = instance_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "channel" or not parts[1] or not parts[2]:
        raise ValueError("Channel instance ID must include its provider and instance")
    now = datetime(2026, 1, 2, tzinfo=UTC)
    intent = OutboundIntent(
        owner_id=owner_id or uuid4(),
        conversation_id=conversation_id or uuid4(),
        channel_instance=ChannelInstanceRef(channel=parts[1], instance_id=parts[2]),
        blocks=[TextBlock(text="Fictional contract probe")],
    )
    policy = DeliveryPolicy.model_validate(adapter.delivery_policy(intent, now=now))
    if not policy.allow_delivery:
        raise AssertionError("Synthetic channel rejected its own text probe")
    result = DeliveryAttempt.model_validate(await adapter.deliver(intent, now=now))
    evidence_rank = {
        DeliveryState.PROVIDER_ACCEPTED: 1,
        DeliveryState.DELIVERED: 2,
        DeliveryState.READ: 3,
    }
    if result.intent_id != intent.intent_id or result.state not in evidence_rank:
        raise AssertionError("Channel must return an evidence-backed delivery state")
    if (
        result.receipt is None
        or result.receipt.intent_id != intent.intent_id
        or evidence_rank.get(result.receipt.state, 0) < evidence_rank[result.state]
    ):
        raise AssertionError("Channel state needs a matching observed receipt")
    if result.rendered is not None and result.rendered.intent_id != intent.intent_id:
        raise AssertionError("Rendered delivery belongs to another intent")
    return {"instance_id": instance_id, "state": result.state.value}


def check_channel_adapter_sync(
    adapter,
    *,
    instance_id: str,
    owner_id: UUID | None = None,
    conversation_id: UUID | None = None,
) -> dict:
    return asyncio.run(
        check_channel_adapter(
            adapter,
            instance_id=instance_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
        )
    )


def check_model_adapter(adapter) -> dict:
    try:
        response = adapter.structured("Return the schema", "Synthetic probe", ModelProbe)
        if not isinstance(response, ModelProbe):
            raise AssertionError("Model did not return a validated synthetic response")
        return {"structured_output": True}
    finally:
        adapter.close()
