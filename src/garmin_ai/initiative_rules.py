"""Tracker-defined check-ins with one cross-channel policy and durable outbox."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import select

from garmin_ai.accounts import owner
from garmin_ai.channels import ChannelInstanceRef, DeliveryState, OutboundIntent, TextBlock
from garmin_ai.dialogue import queue_intent
from garmin_ai.events import StrictModel
from garmin_ai.models import (
    AppState,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    OutboxMessage,
    TrackerConfig,
)
from garmin_ai.normalize import upsert

RULE_PREFIX = "initiative:rule:"


class RuleDefinition(StrictModel):
    kind: Literal["schedule", "missing_entry", "open_interval", "threshold"]
    prompt: str = Field(min_length=1, max_length=500)
    local_time: time | None = None
    threshold_field_id: str | None = None
    threshold_operator: Literal["gt", "gte", "lt", "lte"] | None = None
    threshold_value: float | None = None

    @model_validator(mode="after")
    def complete_rule(self):
        if self.kind in {"schedule", "missing_entry"} and self.local_time is None:
            raise ValueError("Scheduled rules require a local time")
        if self.kind == "threshold" and (
            self.threshold_field_id is None
            or self.threshold_operator is None
            or self.threshold_value is None
        ):
            raise ValueError("Threshold rules require an explicit field, operator, and value")
        return self


class TrackerRuleInstance(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    definition_version_id: UUID
    topic: str = Field(min_length=1, max_length=160)
    rule: RuleDefinition
    conversation_id: UUID
    primary_channel: ChannelInstanceRef
    fallback_channels: list[ChannelInstanceRef] = Field(default_factory=list, max_length=3)
    timezone: str = "UTC"
    enabled: bool = True
    consented: bool = False
    snoozed_until: AwareDatetime | None = None
    quiet_start: time = time(22, 0)
    quiet_end: time = time(7, 0)
    daily_budget: int = Field(default=3, ge=0, le=20)

    @model_validator(mode="after")
    def valid_timezone(self):
        ZoneInfo(self.timezone)
        return self


def save_rule(session, instance: TrackerRuleInstance) -> TrackerRuleInstance:
    upsert(
        session,
        AppState,
        {"key": RULE_PREFIX + str(instance.id), "value": instance.model_dump(mode="json")},
        ["key"],
    )
    if not instance.enabled:
        cancel_queued_for_rule(session, instance.id)
    return instance


def load_rule(session, rule_id: UUID) -> TrackerRuleInstance | None:
    row = session.get(AppState, RULE_PREFIX + str(rule_id))
    return TrackerRuleInstance.model_validate(row.value) if row else None


def cancel_queued_for_rule(session, rule_id: UUID) -> int:
    marker = "rule:" + str(rule_id)
    count = 0
    for row in session.scalars(
        select(OutboxMessage).where(OutboxMessage.state == DeliveryState.QUEUED.value)
    ):
        if marker in row.intent.get("evidence_refs", []):
            row.state = DeliveryState.CANCELLED.value
            row.next_attempt_at = None
            count += 1
    session.flush()
    return count


def _active_tracker(session, instance):
    version = session.get(EventDefinitionVersion, instance.definition_version_id)
    definition = session.get(EventDefinition, version.definition_id) if version else None
    tracker = (
        session.scalar(select(TrackerConfig).where(TrackerConfig.definition_id == definition.id))
        if definition is not None
        else None
    )
    if (
        definition is None
        or tracker is None
        or definition.status != "active"
        or definition.current_version != version.version
    ):
        return None
    return definition, version, tracker


def _quiet_retry(instance, now):
    local = now.astimezone(ZoneInfo(instance.timezone))
    clock = local.timetz().replace(tzinfo=None)
    quiet = (
        instance.quiet_start <= clock < instance.quiet_end
        if instance.quiet_start < instance.quiet_end
        else clock >= instance.quiet_start or clock < instance.quiet_end
    )
    if not quiet:
        return None
    target = datetime.combine(local.date(), instance.quiet_end, ZoneInfo(instance.timezone))
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(UTC)


def _has_entry_today(session, definition_id, instance, now):
    zone = ZoneInfo(instance.timezone)
    local = now.astimezone(zone)
    left = datetime.combine(local.date(), time.min, zone).astimezone(UTC)
    right = left + timedelta(days=1)
    return (
        session.scalar(
            select(Event.id)
            .join(
                EventDefinitionVersion,
                Event.definition_version_id == EventDefinitionVersion.id,
            )
            .where(
                EventDefinitionVersion.definition_id == definition_id,
                Event.deleted.is_(False),
                Event.start >= left,
                Event.start < right,
            )
            .limit(1)
        )
        is not None
    )


def queue_due_checkin(session, rule_id: UUID, now: datetime) -> OutboxMessage | None:
    instance = load_rule(session, rule_id)
    if instance is None or not instance.enabled or not instance.consented:
        return None
    if instance.snoozed_until is not None and instance.snoozed_until > now:
        return None
    active = _active_tracker(session, instance)
    if active is None:
        cancel_queued_for_rule(session, rule_id)
        return None
    definition, _version, _tracker = active
    local = now.astimezone(ZoneInfo(instance.timezone))
    if (
        instance.rule.local_time is not None
        and local.timetz().replace(tzinfo=None) < instance.rule.local_time
    ):
        return None
    if instance.rule.kind == "missing_entry" and _has_entry_today(
        session, definition.id, instance, now
    ):
        return None
    date_key = local.date().isoformat()
    marker = "rule:" + str(rule_id)
    already = session.scalar(
        select(OutboxMessage).where(OutboxMessage.dedup_key == f"{marker}:{date_key}")
    )
    if already is not None:
        return already
    queued_today = sum(
        1
        for row in session.scalars(select(OutboxMessage))
        if row.created_at.astimezone(ZoneInfo(instance.timezone)).date() == local.date()
        and any(ref.startswith("rule:") for ref in row.intent.get("evidence_refs", []))
        and row.state != DeliveryState.CANCELLED.value
    )
    if queued_today >= instance.daily_budget:
        return None
    intent = OutboundIntent(
        owner_id=owner(session).id,
        conversation_id=instance.conversation_id,
        channel_instance=instance.primary_channel,
        blocks=[TextBlock(text=instance.rule.prompt)],
        evidence_refs=[marker, f"definition:{definition.id}"],
        initiative=True,
    )
    row = queue_intent(
        session,
        intent,
        operation_id=uuid4(),
        dedup_key=f"{marker}:{date_key}",
    )
    row.next_attempt_at = _quiet_retry(instance, now)
    session.flush()
    return row


def revalidate_before_send(session, row: OutboxMessage, now: datetime) -> OutboxMessage:
    marker = next(
        (ref for ref in row.intent.get("evidence_refs", []) if ref.startswith("rule:")),
        None,
    )
    if marker is None:
        return row
    instance = load_rule(session, UUID(marker.removeprefix("rule:")))
    if (
        instance is None
        or not instance.enabled
        or not instance.consented
        or _active_tracker(session, instance) is None
    ):
        row.state = DeliveryState.CANCELLED.value
        row.next_attempt_at = None
    else:
        retry = _quiet_retry(instance, now)
        if retry is not None:
            row.state = DeliveryState.QUEUED.value
            row.next_attempt_at = retry
    session.flush()
    return row


def reroute_failed(session, row: OutboxMessage, *, now: datetime) -> OutboxMessage | None:
    """Fallback is explicit and only allowed after a known failure."""

    if row.state != DeliveryState.FAILED.value:
        return None
    marker = next(
        (ref for ref in row.intent.get("evidence_refs", []) if ref.startswith("rule:")),
        None,
    )
    instance = load_rule(session, UUID(marker.removeprefix("rule:"))) if marker else None
    if instance is None or not instance.fallback_channels:
        return None
    intent = OutboundIntent.model_validate(row.intent).model_copy(
        update={
            "intent_id": uuid4(),
            "channel_instance": instance.fallback_channels[0],
        }
    )
    return queue_intent(
        session,
        intent,
        operation_id=row.operation_id,
        dedup_key=row.dedup_key + ":fallback:1",
    )
