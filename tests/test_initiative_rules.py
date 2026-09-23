from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from garmin_ai.accounts import owner
from garmin_ai.channels import ChannelInstanceRef, DeliveryState
from garmin_ai.config import Settings
from garmin_ai.definitions import activate_definition, propose_definition_revision
from garmin_ai.initiative_rules import (
    RuleDefinition,
    TrackerRuleInstance,
    claim_due_initiative,
    queue_due_checkin,
    reroute_failed,
    revalidate_before_send,
    save_rule,
    sync_tracker_rules,
)
from garmin_ai.models import (
    AppState,
    Conversation,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    OutboxMessage,
    PendingQuestion,
    TrackerConfig,
)
from garmin_ai.share_policy import (
    TrackerShareConsent,
    grant_tracker_share,
    revoke_tracker_share,
)
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    definition_spec,
    preview_tracker,
)

NOW = datetime(2026, 9, 20, 20, tzinfo=UTC)


def configured_rule(db, *, topology="point", key="focus", privacy="private", **changes):
    draft = TrackerSetupDraft(
        key=key,
        name="Focus",
        locale="en",
        topology=topology,
        privacy=privacy,
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


def test_sensitive_tracker_without_channel_consent_is_not_queued(db):
    instance = configured_rule(db, privacy="sensitive")
    version = db.get(EventDefinitionVersion, instance.definition_version_id)

    assert queue_due_checkin(db, instance.id, NOW) is None

    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=version.definition_id,
            destination_kind="channel",
            destination_instance_id="restricted-test:primary",
            categories={"schema", "facts"},
            granted_at=NOW,
        ),
        authorized=True,
    )
    assert queue_due_checkin(db, instance.id, NOW) is not None


def test_tracker_rules_use_onboarding_selected_channel(db):
    instance = configured_rule(db)
    version = db.get(EventDefinitionVersion, instance.definition_version_id)
    tracker = db.scalar(
        select(TrackerConfig).where(TrackerConfig.definition_id == version.definition_id)
    )
    tracker.reminder_enabled = True
    tracker.reminder_time = "19:00"
    tracker.reminder_timezone = "UTC"
    selected = Conversation(
        id=uuid4(),
        owner_id=owner(db).id,
        channel="telegram",
        channel_instance_id="selected",
        external_conversation_id="selected-chat",
        memory_epoch=uuid4(),
        state={},
    )
    db.add_all(
        [
            selected,
            AppState(
                key="preferences:onboarding",
                value={"channel": {"channel": "telegram", "instance_id": "selected"}},
            ),
        ]
    )
    db.flush()

    rules = sync_tracker_rules(db, Settings())

    projected = next(row for row in rules if row.definition_version_id == version.id)
    assert projected.conversation_id == selected.id
    assert projected.primary_channel == ChannelInstanceRef(
        channel="telegram", instance_id="selected"
    )
    assert projected.fallback_channels == []


def test_tracker_rules_include_only_onboarding_opted_in_fallbacks(db):
    instance = configured_rule(db)
    version = db.get(EventDefinitionVersion, instance.definition_version_id)
    tracker = db.scalar(
        select(TrackerConfig).where(TrackerConfig.definition_id == version.definition_id)
    )
    tracker.reminder_enabled = True
    tracker.reminder_time = "19:00"
    tracker.reminder_timezone = "UTC"
    person = owner(db)
    primary = Conversation(
        id=uuid4(),
        owner_id=person.id,
        channel="telegram",
        channel_instance_id="selected",
        external_conversation_id="selected-chat",
        memory_epoch=uuid4(),
        state={},
    )
    opted_in = Conversation(
        id=uuid4(),
        owner_id=person.id,
        channel="restricted-test",
        channel_instance_id="fallback",
        external_conversation_id="fallback-chat",
        memory_epoch=uuid4(),
        state={},
    )
    ignored = Conversation(
        id=uuid4(),
        owner_id=person.id,
        channel="restricted-test",
        channel_instance_id="not-selected",
        external_conversation_id="ignored-chat",
        memory_epoch=uuid4(),
        state={},
    )
    db.add_all(
        [
            primary,
            opted_in,
            ignored,
            AppState(
                key="preferences:onboarding",
                value={
                    "channel": {"channel": "telegram", "instance_id": "selected"},
                    "fallback_channels": [
                        {"channel": "restricted-test", "instance_id": "fallback"}
                    ],
                },
            ),
        ]
    )
    db.flush()

    projected = next(
        row for row in sync_tracker_rules(db, Settings()) if row.definition_version_id == version.id
    )

    assert projected.fallback_channels == [
        ChannelInstanceRef(channel="restricted-test", instance_id="fallback")
    ]

    tracker.reminder_enabled = False
    assert sync_tracker_rules(db, Settings()) == []
    tracker.reminder_enabled = True

    restored = next(
        row for row in sync_tracker_rules(db, Settings()) if row.definition_version_id == version.id
    )
    assert restored.enabled


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


def test_deferred_missing_entry_revalidates_the_scheduled_local_date(db):
    instance = configured_rule(db)
    queued_at = datetime(2026, 9, 20, 23, tzinfo=UTC)
    row = queue_due_checkin(db, instance.id, queued_at)
    db.add(
        Event(
            definition_version_id=instance.definition_version_id,
            kind="user.focus",
            start=queued_at + timedelta(minutes=30),
            end=None,
            timezone="UTC",
            source="manual",
            payload={"quality": 3},
            topology="point",
        )
    )
    db.flush()

    result = revalidate_before_send(db, row, queued_at + timedelta(minutes=45))

    assert result.state == DeliveryState.CANCELLED.value


def test_date_bound_checkin_expires_after_its_local_day(db):
    instance = configured_rule(db)
    queued_at = datetime(2026, 9, 20, 23, tzinfo=UTC)
    row = queue_due_checkin(db, instance.id, queued_at)

    result = revalidate_before_send(db, row, queued_at + timedelta(hours=9))

    assert result.state == DeliveryState.EXPIRED.value


def test_tracker_without_create_permission_does_not_schedule_reminders(db):
    instance = configured_rule(db)
    version = db.get(EventDefinitionVersion, instance.definition_version_id)
    definition = db.get(EventDefinition, version.definition_id)
    draft = TrackerSetupDraft(
        key="focus",
        name="Focus",
        locale="en",
        topology="point",
        fields=[
            TrackerFieldDraft(key="quality", label="Quality", kind="scale", minimum=1, maximum=5)
        ],
        shortcut="Log focus",
    )
    restricted = definition_spec(draft).model_copy(
        update={"allowed_operations": {"query", "update", "delete"}}
    )
    proposed = propose_definition_revision(
        db,
        definition.id,
        definition.revision,
        restricted,
        actor="test",
        authorized=True,
    )
    activate_definition(db, definition.id, proposed.revision, actor="test", authorized=True)
    save_rule(db, instance.model_copy(update={"definition_version_id": proposed.id}))

    assert queue_due_checkin(db, instance.id, NOW) is None


def test_equal_quiet_hour_endpoints_do_not_defer_checkins(db):
    instance = configured_rule(db, quiet_start=time(0, 0), quiet_end=time(0, 0))

    row = queue_due_checkin(db, instance.id, NOW)

    assert row is not None
    assert row.next_attempt_at is None


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


def test_recent_previous_day_checkin_is_recovered_after_midnight(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
    )
    restarted_at = datetime(2026, 9, 21, 1, tzinfo=UTC)

    row = queue_due_checkin(db, instance.id, restarted_at)

    assert row is not None
    assert row.dedup_key.endswith(":2026-09-20")


def test_question_budget_is_validated_before_rule_projection():
    with pytest.raises(ValueError):
        Settings(question_budget=21)


def test_tracker_checkin_uses_shared_notification_budget(db):
    instance = configured_rule(db, daily_budget=1)
    db.add(
        PendingQuestion(
            kind="synthetic",
            text="Already sent",
            evidence={},
            priority=1,
            earliest_send_at=NOW,
            expires_at=NOW + timedelta(days=1),
            sent_at=NOW,
            status="sent",
            dedup_key="already-sent",
        )
    )
    db.flush()

    assert queue_due_checkin(db, instance.id, NOW) is None


def test_claim_recovers_expired_initiative_lease_as_uncertain(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    row.state = DeliveryState.SENDING.value
    row.lease_token = uuid4()
    row.lease_until = NOW - timedelta(seconds=1)
    db.flush()

    assert claim_due_initiative(db, NOW) is None
    assert row.state == DeliveryState.UNCERTAIN.value
    assert row.lease_token is None
    assert row.lease_until is None


def test_pending_clarification_defers_neutral_initiatives(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    db.add(AppState(key="conversation:pending", value={"created_at": NOW.isoformat()}))
    db.flush()

    assert claim_due_initiative(db, NOW) is None
    assert row.state == DeliveryState.QUEUED.value


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


def test_channel_fallback_advances_once_through_the_entire_chain(db):
    instance = configured_rule(
        db,
        fallback_channels=[
            ChannelInstanceRef(channel="telegram", instance_id="first"),
            ChannelInstanceRef(channel="telegram", instance_id="second"),
        ],
    )
    row = queue_due_checkin(db, instance.id, NOW)
    row.state = DeliveryState.FAILED.value

    first = reroute_failed(db, row, now=NOW)
    first.state = DeliveryState.FAILED.value
    second = reroute_failed(db, first, now=NOW)
    second.state = DeliveryState.FAILED.value

    assert first.intent["channel_instance"]["instance_id"] == "first"
    assert second.intent["channel_instance"]["instance_id"] == "second"
    assert reroute_failed(db, second, now=NOW) is None


def test_rule_synchronization_preserves_owner_disable_and_snooze(db):
    configured_rule(db)
    tracker = db.scalar(select(TrackerConfig))
    tracker.reminder_enabled = True
    tracker.reminder_time = "19:00"
    tracker.reminder_timezone = "UTC"
    generated = sync_tracker_rules(db, Settings())[0]
    snoozed_until = NOW + timedelta(days=2)
    save_rule(db, generated.model_copy(update={"enabled": False, "snoozed_until": snoozed_until}))

    refreshed = sync_tracker_rules(db, Settings())[0]

    assert not refreshed.enabled
    assert refreshed.snoozed_until == snoozed_until


def test_snooze_added_after_queue_defers_pre_send_delivery(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    snoozed_until = NOW + timedelta(hours=3)
    save_rule(db, instance.model_copy(update={"snoozed_until": snoozed_until}))

    revalidate_before_send(db, row, NOW + timedelta(minutes=1))

    assert row.state == DeliveryState.QUEUED.value
    assert row.next_attempt_at == snoozed_until


def test_daily_checkin_is_expired_after_its_local_day(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)

    revalidate_before_send(db, row, NOW + timedelta(days=1))

    assert row.state == DeliveryState.EXPIRED.value


def test_expired_initiative_lease_is_fenced_as_uncertain(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    lease = claim_due_initiative(db, NOW)

    assert lease is not None and lease.outbox_message_id == row.id
    assert claim_due_initiative(db, NOW + timedelta(minutes=3)) is None
    assert row.state == DeliveryState.UNCERTAIN.value
    assert row.lease_token is None and row.lease_until is None


def test_unconsented_sensitive_checkin_is_skipped_without_aborting_cycle(db):
    sensitive = configured_rule(db, key="sensitive", privacy="sensitive")
    ordinary = configured_rule(db, key="ordinary")

    rows = [
        row
        for instance in (sensitive, ordinary)
        if (row := queue_due_checkin(db, instance.id, NOW)) is not None
    ]

    assert len(rows) == 1
    assert f"rule:{ordinary.id}" in rows[0].intent["evidence_refs"]

    version = db.get(EventDefinitionVersion, sensitive.definition_version_id)
    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=version.definition_id,
            destination_kind="channel",
            destination_instance_id="restricted-test:primary",
            categories={"schema", "facts"},
            granted_at=NOW,
        ),
        authorized=True,
    )
    queued = queue_due_checkin(db, sensitive.id, NOW)
    assert queued is not None
    revoke_tracker_share(
        db,
        version.definition_id,
        "channel",
        "restricted-test:primary",
        authorized=True,
    )
    assert revalidate_before_send(db, queued, NOW).state == DeliveryState.CANCELLED.value


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
