"""Tracker-defined check-ins with one cross-channel policy and durable outbox."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.accounts import owner
from garmin_ai.channels import (
    ChannelInstanceRef,
    DeliveryAttempt,
    DeliveryReceipt,
    DeliveryState,
    OutboundIntent,
    TextBlock,
)
from garmin_ai.dialogue import queue_intent, record_delivery_receipt
from garmin_ai.events import StrictModel
from garmin_ai.i18n import translate
from garmin_ai.models import (
    AppState,
    ChannelBinding,
    Conversation,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    OutboxMessage,
    TrackerConfig,
)
from garmin_ai.normalize import upsert

RULE_PREFIX = "initiative:rule:"
TRACKER_RULE_NAMESPACE = UUID("68af2e31-b719-45a1-a934-92ae9c92091b")
TELEGRAM_CONVERSATION_NAMESPACE = UUID("5ddd62fc-6890-44b6-86a2-20f77524378f")


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


class InitiativeLease(StrictModel):
    outbox_message_id: UUID
    lease_token: UUID
    intent: OutboundIntent


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
        or "create" not in version.allowed_operations
    ):
        return None
    return definition, version, tracker


def _quiet_retry(instance, now):
    if instance.quiet_start == instance.quiet_end:
        return None
    local = now.astimezone(ZoneInfo(instance.timezone))
    clock = local.timetz().replace(tzinfo=None)
    if instance.quiet_start == instance.quiet_end:
        return None
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


def _has_entry_on_date(session, definition_id, instance, local_date: date):
    zone = ZoneInfo(instance.timezone)
    left = datetime.combine(local_date, time.min, zone).astimezone(UTC)
    right = datetime.combine(local_date + timedelta(days=1), time.min, zone).astimezone(UTC)
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


def _rule_condition_matches(session, definition, version, instance, now, *, scheduled_day=None):
    if instance.rule.kind == "missing_entry":
        local_date = scheduled_day or now.astimezone(ZoneInfo(instance.timezone)).date()
        return not _has_entry_on_date(session, definition.id, instance, local_date)
    if instance.rule.kind == "open_interval":
        return (
            session.scalar(
                select(Event.id)
                .join(
                    EventDefinitionVersion,
                    Event.definition_version_id == EventDefinitionVersion.id,
                )
                .where(
                    EventDefinitionVersion.definition_id == definition.id,
                    Event.deleted.is_(False),
                    Event.topology == "open_interval",
                    Event.end.is_(None),
                    Event.start <= now,
                )
                .limit(1)
            )
            is not None
        )
    if instance.rule.kind == "threshold":
        field = next(
            (
                name
                for name, metadata in version.field_metadata.items()
                if metadata.get("id") == instance.rule.threshold_field_id
            ),
            None,
        )
        if field is None:
            return False
        event = session.scalar(
            select(Event)
            .join(
                EventDefinitionVersion,
                Event.definition_version_id == EventDefinitionVersion.id,
            )
            .where(
                EventDefinitionVersion.definition_id == definition.id,
                Event.deleted.is_(False),
                Event.start <= now,
            )
            .order_by(Event.start.desc(), Event.id.desc())
            .limit(1)
        )
        value = event.payload.get(field) if event is not None else None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        threshold = instance.rule.threshold_value
        return {
            "gt": value > threshold,
            "gte": value >= threshold,
            "lt": value < threshold,
            "lte": value <= threshold,
        }[instance.rule.threshold_operator]
    return True


def sync_tracker_rules(session, settings) -> list[TrackerRuleInstance]:
    """Project enabled tracker reminders into stable channel-neutral rules."""

    from garmin_ai.integrations import channel_instance_id, configured_instance

    person = owner(session)
    telegram_instance_id = channel_instance_id(configured_instance(settings, "channel", "telegram"))
    telegram_binding = session.scalar(
        select(ChannelBinding).where(
            ChannelBinding.owner_id == person.id,
            ChannelBinding.channel == "telegram",
            ChannelBinding.channel_instance_id == telegram_instance_id,
        )
    )
    if (
        telegram_binding is not None
        and session.scalar(
            select(Conversation.id)
            .where(
                Conversation.owner_id == person.id,
                Conversation.channel == "telegram",
                Conversation.channel_instance_id == telegram_instance_id,
            )
            .limit(1)
        )
        is None
    ):
        session.execute(
            insert(Conversation)
            .values(
                id=uuid5(
                    TELEGRAM_CONVERSATION_NAMESPACE,
                    f"{person.id}:telegram:{telegram_instance_id}:{telegram_binding.external_id}",
                ),
                owner_id=person.id,
                channel="telegram",
                channel_instance_id=telegram_instance_id,
                external_conversation_id=telegram_binding.external_id,
                memory_epoch=uuid4(),
                state={},
                share_owner_memory=False,
            )
            .on_conflict_do_nothing(index_elements=[Conversation.id])
        )
    conversations = session.scalars(
        select(Conversation)
        .where(Conversation.owner_id == person.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id)
    ).all()
    onboarding = session.get(AppState, "preferences:onboarding")
    selected_channel = onboarding.value.get("channel") if onboarding is not None else None
    selected_fallbacks = (
        onboarding.value.get("fallback_channels", []) if onboarding is not None else []
    )
    configured = session.execute(
        select(TrackerConfig, EventDefinition, EventDefinitionVersion)
        .join(EventDefinition, EventDefinition.id == TrackerConfig.definition_id)
        .join(
            EventDefinitionVersion,
            (EventDefinitionVersion.definition_id == EventDefinition.id)
            & (EventDefinitionVersion.version == EventDefinition.current_version),
        )
        .where(TrackerConfig.owner_id == person.id)
    ).all()
    rules = []
    for tracker, definition, version in configured:
        rule_id = uuid5(TRACKER_RULE_NAMESPACE, str(tracker.id))
        existing = load_rule(session, rule_id)
        active = (
            tracker.reminder_enabled
            and tracker.reminder_time is not None
            and tracker.reminder_timezone is not None
            and definition.status == "active"
        )
        if not active or not conversations:
            if existing is not None:
                cancel_queued_for_rule(session, existing.id)
            continue
        if selected_channel is not None:
            selected = next(
                (
                    row
                    for row in conversations
                    if row.channel == selected_channel.get("channel")
                    and row.channel_instance_id == selected_channel.get("instance_id")
                ),
                None,
            )
            if selected is None:
                if existing is not None:
                    cancel_queued_for_rule(session, existing.id)
                continue
        else:
            selected = next(
                (
                    row
                    for row in conversations
                    if row.id == getattr(existing, "conversation_id", None)
                ),
                conversations[0],
            )
        primary = ChannelInstanceRef(
            channel=selected.channel,
            instance_id=selected.channel_instance_id,
        )
        conversation_channels = {
            (row.channel, row.channel_instance_id) for row in conversations if row.id != selected.id
        }
        fallbacks = [
            channel
            for raw in selected_fallbacks
            if (channel := ChannelInstanceRef.model_validate(raw)).namespace
            in conversation_channels
        ]
        hour, minute = (int(part) for part in tracker.reminder_time.split(":"))
        candidate = TrackerRuleInstance(
            id=rule_id,
            definition_version_id=version.id,
            topic=f"tracker:{tracker.id}",
            rule=RuleDefinition(
                kind="missing_entry",
                prompt=translate(
                    "tracker.reminder",
                    person.locale,
                    tracker=tracker.shortcut or definition.key,
                ),
                local_time=time(hour, minute),
            ),
            conversation_id=selected.id,
            primary_channel=primary,
            fallback_channels=fallbacks,
            timezone=tracker.reminder_timezone,
            enabled=existing.enabled if existing is not None else True,
            consented=existing.consented if existing is not None else True,
            snoozed_until=existing.snoozed_until if existing is not None else None,
            quiet_start=time(settings.quiet_start_hour),
            quiet_end=time(settings.quiet_end_hour),
            daily_budget=settings.question_budget,
        )
        if existing is None or existing != candidate:
            save_rule(session, candidate)
        rules.append(candidate)
    return rules


def queue_due_tracker_checkins(session, settings, now) -> list[OutboxMessage]:
    return [
        row
        for instance in sync_tracker_rules(session, settings)
        if (row := queue_due_checkin(session, instance.id, now)) is not None
    ]


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
    definition, version, _tracker = active
    from garmin_ai.share_policy import sharing_allowed

    if not sharing_allowed(
        session,
        definition.id,
        destination_kind="channel",
        destination_instance_id=(
            f"{instance.primary_channel.channel}:{instance.primary_channel.instance_id}"
        ),
        categories={"schema", "facts"},
    ):
        return None
    local = now.astimezone(ZoneInfo(instance.timezone))
    scheduled_day = local.date()
    if instance.rule.local_time is not None:
        scheduled_at = datetime.combine(scheduled_day, instance.rule.local_time, local.tzinfo)
        if local < scheduled_at:
            previous_due = scheduled_at - timedelta(days=1)
            if local - previous_due > timedelta(hours=12):
                return None
            scheduled_day = previous_due.date()
    if not _rule_condition_matches(
        session,
        definition,
        version,
        instance,
        now,
        scheduled_day=scheduled_day,
    ):
        return None
    date_key = scheduled_day.isoformat()
    marker = "rule:" + str(rule_id)
    already = session.scalar(
        select(OutboxMessage).where(OutboxMessage.dedup_key == f"{marker}:{date_key}")
    )
    if already is not None:
        return already
    session.execute(select(func.pg_advisory_xact_lock(72104621)))
    from garmin_ai.proactive import notification_count

    if notification_count(session, instance, now) >= instance.daily_budget:
        return None
    intent = OutboundIntent(
        owner_id=owner(session).id,
        conversation_id=instance.conversation_id,
        channel_instance=instance.primary_channel,
        blocks=[TextBlock(text=instance.rule.prompt)],
        evidence_refs=[marker, f"definition:{definition.id}"],
        initiative=True,
        expires_at=(
            datetime.combine(local.date() + timedelta(days=1), time.min, local.tzinfo).astimezone(
                UTC
            )
            if instance.rule.kind == "missing_entry"
            else None
        ),
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
    try:
        scheduled_day = date.fromisoformat(row.dedup_key.rsplit(":", 1)[-1])
    except ValueError:
        scheduled_day = None
    instance = load_rule(session, UUID(marker.removeprefix("rule:")))
    active = _active_tracker(session, instance) if instance is not None else None
    sharing_still_allowed = False
    if active is not None:
        from garmin_ai.share_policy import sharing_allowed

        destination = OutboundIntent.model_validate(row.intent).channel_instance
        sharing_still_allowed = sharing_allowed(
            session,
            active[0].id,
            destination_kind="channel",
            destination_instance_id=f"{destination.channel}:{destination.instance_id}",
            categories={"schema", "facts"},
        )
    if (
        instance is None
        or not instance.enabled
        or not instance.consented
        or active is None
        or not sharing_still_allowed
    ):
        row.state = DeliveryState.CANCELLED.value
        row.next_attempt_at = None
        session.flush()
        return row
    if (
        scheduled_day is not None
        and scheduled_day < now.astimezone(ZoneInfo(instance.timezone)).date()
    ):
        row.state = DeliveryState.EXPIRED.value
        row.next_attempt_at = None
        session.flush()
        return row
    if not _rule_condition_matches(
        session,
        active[0],
        active[1],
        instance,
        now,
        scheduled_day=scheduled_day,
    ):
        row.state = DeliveryState.CANCELLED.value
        row.next_attempt_at = None
    elif instance.snoozed_until is not None and instance.snoozed_until > now:
        row.state = DeliveryState.QUEUED.value
        row.next_attempt_at = instance.snoozed_until
    else:
        retry = _quiet_retry(instance, now)
        if retry is not None:
            row.state = DeliveryState.QUEUED.value
            row.next_attempt_at = retry
    session.flush()
    return row


def claim_due_initiative(session, now: datetime) -> InitiativeLease | None:
    """Claim one revalidated initiative without mixing it with ordinary replies."""

    from garmin_ai.agent import pending_clarification
    from garmin_ai.dialogue import recover_expired_outbox_leases

    if pending_clarification(session, now):
        return None
    recover_expired_outbox_leases(session, now)

    rows = session.scalars(
        select(OutboxMessage)
        .where(
            OutboxMessage.state == DeliveryState.QUEUED.value,
            OutboxMessage.intent["initiative"].as_boolean().is_(True),
            (OutboxMessage.next_attempt_at.is_(None)) | (OutboxMessage.next_attempt_at <= now),
        )
        .order_by(OutboxMessage.created_at, OutboxMessage.id)
        .with_for_update(skip_locked=True)
        .limit(20)
    ).all()
    for row in rows:
        revalidate_before_send(session, row, now)
        if row.state != DeliveryState.QUEUED.value or (
            row.next_attempt_at is not None and row.next_attempt_at > now
        ):
            continue
        token = uuid4()
        row.state = DeliveryState.SENDING.value
        row.lease_token = token
        row.lease_until = now + timedelta(minutes=2)
        row.attempts += 1
        session.flush()
        return InitiativeLease(
            outbox_message_id=row.id,
            lease_token=token,
            intent=OutboundIntent.model_validate(row.intent),
        )
    return None


def finish_initiative_attempt(
    session,
    lease: InitiativeLease,
    attempt: DeliveryAttempt,
    now: datetime,
) -> OutboxMessage:
    row = session.get(OutboxMessage, lease.outbox_message_id, populate_existing=True)
    if row is None or row.lease_token != lease.lease_token:
        raise LookupError("Initiative delivery lease is no longer current")
    if attempt.state is DeliveryState.QUEUED:
        row.state = DeliveryState.QUEUED.value
        row.next_attempt_at = attempt.retry_after or now + timedelta(minutes=15)
        row.lease_token = None
        row.lease_until = None
        session.flush()
        return row
    receipt = attempt.receipt or DeliveryReceipt(
        intent_id=row.id,
        state=attempt.state,
        observed_at=now,
        detail=attempt.reason,
    )
    record_delivery_receipt(session, row.id, receipt, lease_token=lease.lease_token)
    if attempt.state is DeliveryState.FAILED:
        reroute_failed(session, row, now=now)
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
    current = ChannelInstanceRef.model_validate(row.intent["channel_instance"])
    routes = [instance.primary_channel, *instance.fallback_channels]
    try:
        next_index = routes.index(current) + 1
    except ValueError:
        next_index = 1
    if next_index >= len(routes):
        return None
    intent = OutboundIntent.model_validate(row.intent).model_copy(
        update={
            "intent_id": uuid4(),
            "channel_instance": routes[next_index],
        }
    )
    root_key = re.sub(r":fallback:\d+$", "", row.dedup_key)
    return queue_intent(
        session,
        intent,
        operation_id=row.operation_id,
        dedup_key=f"{root_key}:fallback:{next_index}",
    )
