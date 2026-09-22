from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

from sqlalchemy import select

from garmin_ai.accounts import owner
from garmin_ai.channels import ChannelInstanceRef, DeliveryState
from garmin_ai.initiative_rules import (
    RuleDefinition,
    TrackerRuleInstance,
    queue_due_checkin,
    reroute_failed,
    revalidate_before_send,
    save_rule,
)
from garmin_ai.models import Conversation, Event, EventDefinitionVersion, OutboxMessage
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

NOW = datetime(2026, 9, 20, 20, tzinfo=UTC)


def configured_rule(db, *, topology="point", key="focus", **changes):
    draft = TrackerSetupDraft(
        key=key,
        name="Focus",
        locale="en",
        topology=topology,
        fields=[
            TrackerFieldDraft(key="quality", label="Quality", kind="scale", minimum=1, maximum=5)
        ],
        shortcut="Log focus",
    )
    preview = preview_tracker(db, draft)
    created = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    conversation_id = uuid4()
    db.add(
        Conversation(
            id=conversation_id,
            owner_id=owner(db).id,
            channel="restricted-test",
            channel_instance_id="primary",
            external_conversation_id=f"opaque-{key}",
            memory_epoch=uuid4(),
            state={},
        )
    )
    values = dict(
        definition_version_id=created["action"]["definition_version_id"],
        topic=key,
        rule=RuleDefinition(
            kind="missing_entry", prompt="How was your focus?", local_time=time(19, 0)
        ),
        conversation_id=conversation_id,
        primary_channel=ChannelInstanceRef(channel="restricted-test", instance_id="primary"),
        fallback_channels=[ChannelInstanceRef(channel="telegram", instance_id="primary")],
        timezone="UTC",
        consented=True,
        quiet_start=time(23, 0),
        quiet_end=time(6, 0),
    )
    values.update(changes)
    instance = TrackerRuleInstance(**values)
    save_rule(db, instance)
    db.flush()
    return instance


def test_new_tracker_gets_first_checkin_without_legacy_history(db):
    instance = configured_rule(db)

    row = queue_due_checkin(db, instance.id, NOW)

    assert row is not None
    assert row.intent["blocks"][0]["text"] == "How was your focus?"
    assert db.scalar(select(OutboxMessage)) == row


def test_disabling_rule_cancels_queued_intent_and_restart_revalidation(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    save_rule(db, instance.model_copy(update={"enabled": False}))
    db.flush()

    assert row.state == DeliveryState.CANCELLED.value
    assert revalidate_before_send(db, row, NOW).state == DeliveryState.CANCELLED.value


def test_new_entry_after_queue_cancels_missing_entry_before_send(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    db.add(
        Event(
            definition_version_id=instance.definition_version_id,
            kind="user.focus",
            start=NOW - timedelta(minutes=5),
            end=None,
            timezone="UTC",
            source="manual",
            payload={"quality": 3},
            topology="point",
        )
    )
    db.flush()

    assert revalidate_before_send(db, row, NOW).state == DeliveryState.CANCELLED.value


def test_quiet_hours_keep_future_action_instead_of_dropping(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="schedule", prompt="Check in", local_time=time(0, 0)),
        quiet_start=time(19, 0),
        quiet_end=time(21, 0),
    )
    row = queue_due_checkin(db, instance.id, NOW)

    assert row.state == DeliveryState.QUEUED.value
    assert row.next_attempt_at == NOW + timedelta(hours=1)


def test_channel_fallback_requires_known_failure_and_never_duplicates_uncertain(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    row.state = DeliveryState.UNCERTAIN.value
    assert reroute_failed(db, row, now=NOW) is None
    assert db.scalar(select(OutboxMessage).where(OutboxMessage.id != row.id)) is None

    row.state = DeliveryState.FAILED.value
    fallback = reroute_failed(db, row, now=NOW)
    assert fallback.intent["channel_instance"] == {
        "channel": "telegram",
        "instance_id": "primary",
    }
    assert fallback.id != row.id


def test_snooze_added_after_queue_defers_pre_send_delivery(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    snoozed_until = NOW + timedelta(hours=3)
    save_rule(db, instance.model_copy(update={"snoozed_until": snoozed_until}))

    revalidate_before_send(db, row, NOW + timedelta(minutes=1))

    assert row.state == DeliveryState.QUEUED.value
    assert row.next_attempt_at == snoozed_until


def test_missing_entry_day_boundary_uses_next_local_midnight_across_dst(db):
    now = datetime(2026, 3, 29, 12, tzinfo=UTC)
    instance = configured_rule(
        db,
        timezone="Europe/Bratislava",
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(0, 0)),
    )
    # This is 00:30 on the next local day after the 23-hour spring DST day.
    db.add(
        Event(
            definition_version_id=instance.definition_version_id,
            kind="user.focus",
            start=datetime(2026, 3, 29, 22, 30, tzinfo=UTC),
            end=None,
            timezone="Europe/Bratislava",
            source="manual",
            payload={"quality": 3},
            topology="point",
        )
    )
    db.flush()

    assert queue_due_checkin(db, instance.id, now) is not None


def test_open_interval_and_threshold_rules_evaluate_tracker_data(db):
    open_rule = configured_rule(
        db,
        topology="open_interval",
        rule=RuleDefinition(kind="open_interval", prompt="Still active?"),
    )
    db.add(
        Event(
            definition_version_id=open_rule.definition_version_id,
            kind="user.focus",
            start=NOW - timedelta(hours=3),
            end=None,
            timezone="UTC",
            source="manual",
            payload={"quality": 3},
            topology="point",
        )
    )
    db.flush()
    assert queue_due_checkin(db, open_rule.id, NOW) is None
    db.add(
        Event(
            definition_version_id=open_rule.definition_version_id,
            kind="user.focus",
            start=NOW - timedelta(hours=2),
            end=None,
            timezone="UTC",
            source="manual",
            payload={"quality": 3},
            topology="open_interval",
        )
    )
    db.flush()
    assert queue_due_checkin(db, open_rule.id, NOW) is not None

    threshold_rule = configured_rule(db, key="energy")
    version = db.get(EventDefinitionVersion, threshold_rule.definition_version_id)
    field_id = version.field_metadata["quality"]["id"]
    threshold_rule = threshold_rule.model_copy(
        update={
            "rule": RuleDefinition(
                kind="threshold",
                prompt="High focus?",
                threshold_field_id=field_id,
                threshold_operator="gt",
                threshold_value=4,
            )
        }
    )
    save_rule(db, threshold_rule)
    db.add(
        Event(
            definition_version_id=threshold_rule.definition_version_id,
            kind="user.energy",
            start=NOW - timedelta(minutes=5),
            end=None,
            timezone="UTC",
            source="manual",
            payload={"quality": 5},
            topology="point",
        )
    )
    db.flush()

    assert queue_due_checkin(db, threshold_rule.id, NOW) is not None
