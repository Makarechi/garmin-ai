from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from garmin_ai.accounts import owner
from garmin_ai.channels import (
    ChannelInstanceRef,
    DeliveryAttempt,
    DeliveryReceipt,
    DeliveryState,
    OutboundIntent,
)
from garmin_ai.config import Settings
from garmin_ai.definitions import activate_definition, propose_definition_revision
from garmin_ai.dialogue import queue_intent, record_delivery_receipt
from garmin_ai.initiative_rules import (
    RuleDefinition,
    TrackerRuleInstance,
    claim_due_initiative,
    finish_initiative_attempt,
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
    Insight,
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
    TrackerSettingsUpdate,
    TrackerSetupDraft,
    confirm_tracker,
    definition_spec,
    preview_tracker,
    update_tracker_settings,
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


def fallback_conversation(db, channel):
    target = Conversation(
        id=uuid4(),
        owner_id=owner(db).id,
        channel=channel.channel,
        channel_instance_id=channel.instance_id,
        external_conversation_id=f"fallback-{channel.instance_id}-{uuid4()}",
        memory_epoch=uuid4(),
        state={},
    )
    db.add(target)
    db.flush()
    return target


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


def test_rule_revision_can_queue_new_checkin_on_same_day(db):
    instance = configured_rule(db)
    original = queue_due_checkin(db, instance.id, NOW)

    revised = instance.model_copy(
        update={"rule": instance.rule.model_copy(update={"prompt": "How is your focus now?"})}
    )
    save_rule(db, revised)
    replacement = queue_due_checkin(db, instance.id, NOW)

    assert original.state == DeliveryState.CANCELLED.value
    assert replacement is not None and replacement.id != original.id
    assert replacement.state == DeliveryState.QUEUED.value
    assert replacement.intent["blocks"][0]["text"] == "How is your focus now?"
    assert replacement.dedup_key != original.dedup_key
    assert queue_due_checkin(db, instance.id, NOW) is replacement


@pytest.mark.parametrize(
    "state",
    [
        DeliveryState.SENDING.value,
        DeliveryState.PROVIDER_ACCEPTED.value,
        DeliveryState.DELIVERED.value,
        DeliveryState.UNCERTAIN.value,
    ],
)
def test_rule_revision_does_not_repeat_consumed_daily_occurrence(db, state):
    instance = configured_rule(db)
    original = queue_due_checkin(db, instance.id, NOW)
    original.state = state
    revised = instance.model_copy(
        update={"rule": instance.rule.model_copy(update={"prompt": "A revised prompt"})}
    )
    save_rule(db, revised)

    same_day = queue_due_checkin(db, instance.id, NOW)

    assert same_day.id == original.id
    assert db.scalar(select(func.count()).select_from(OutboxMessage)) == 1


def test_disabling_rule_cancels_queued_intent_and_restart_revalidation(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    save_rule(db, instance.model_copy(update={"enabled": False}))
    db.flush()

    assert row.state == DeliveryState.CANCELLED.value
    assert revalidate_before_send(db, row, NOW).state == DeliveryState.CANCELLED.value


def test_owner_pause_cancels_queued_checkin_before_claim(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    db.add(AppState(key="proactive:enabled", value={"enabled": False}))
    db.flush()

    assert claim_due_initiative(db, NOW) is None
    assert row.state == DeliveryState.CANCELLED.value


def test_generators_hold_pause_policy_lock(db, db_engine):
    from garmin_ai.proactive import generate_insights, generate_questions

    db.add(AppState(key="proactive:enabled", value={"enabled": False}))
    db.flush()
    generate_questions(db, Settings(), NOW)
    generate_insights(db, NOW, "UTC")

    with db_engine.connect() as connection:
        assert connection.scalar(text("SELECT pg_try_advisory_xact_lock(72104621)")) is False


def test_disabling_tracker_reminder_cancels_queued_checkin_before_rule_sync(db):
    instance = configured_rule(db)
    tracker = db.scalar(select(TrackerConfig))
    tracker.reminder_enabled = True
    tracker.reminder_time = "19:00"
    tracker.reminder_timezone = "UTC"
    save_rule(db, instance.model_copy(update={"topic": f"tracker:{tracker.id}"}))
    row = queue_due_checkin(db, instance.id, NOW)
    tracker.reminder_enabled = False
    db.flush()

    assert claim_due_initiative(db, NOW) is None
    assert row.state == DeliveryState.CANCELLED.value


@pytest.mark.parametrize(
    "change",
    [
        {"reminder_enabled": False, "reminder_time": None},
        {"reminder_time": "21:00"},
        {"reminder_timezone": "Europe/Budapest"},
        {"shortcut": "Updated shortcut"},
    ],
)
def test_tracker_settings_cancel_projected_checkin_immediately(db, change):
    instance = configured_rule(db)
    version = db.get(EventDefinitionVersion, instance.definition_version_id)
    tracker = db.scalar(
        select(TrackerConfig).where(TrackerConfig.definition_id == version.definition_id)
    )
    tracker.reminder_enabled = True
    tracker.reminder_time = "19:00"
    tracker.reminder_timezone = "UTC"
    projected = next(
        rule
        for rule in sync_tracker_rules(db, Settings())
        if rule.definition_version_id == version.id
    )
    row = queue_due_checkin(db, projected.id, NOW)
    assert row is not None

    update_tracker_settings(
        db,
        tracker.id,
        TrackerSettingsUpdate(
            revision=tracker.revision,
            shortcut=change.get("shortcut", tracker.shortcut),
            reminder_enabled=change.get("reminder_enabled", True),
            reminder_time=change.get("reminder_time", "19:00"),
            reminder_timezone=change.get("reminder_timezone", "UTC"),
        ),
    )

    assert row.state == DeliveryState.CANCELLED.value
    assert claim_due_initiative(db, NOW) is None


def test_identical_tracker_settings_preserve_queued_checkin(db):
    instance = configured_rule(db)
    version = db.get(EventDefinitionVersion, instance.definition_version_id)
    tracker = db.scalar(
        select(TrackerConfig).where(TrackerConfig.definition_id == version.definition_id)
    )
    tracker.reminder_enabled = True
    tracker.reminder_time = "19:00"
    tracker.reminder_timezone = "UTC"
    projected = next(
        rule
        for rule in sync_tracker_rules(db, Settings())
        if rule.definition_version_id == version.id
    )
    row = queue_due_checkin(db, projected.id, NOW)
    revision = tracker.revision

    update_tracker_settings(
        db,
        tracker.id,
        TrackerSettingsUpdate(
            revision=revision,
            shortcut=tracker.shortcut,
            reminder_enabled=True,
            reminder_time="19:00",
            reminder_timezone="UTC",
        ),
    )

    assert tracker.revision == revision
    assert row.state == DeliveryState.QUEUED.value
    assert claim_due_initiative(db, NOW).outbox_message_id == row.id


def test_pause_cancels_queued_initiatives_and_resume_does_not_replay_them(db, db_engine):
    from garmin_ai.events import EventInput, create_event
    from garmin_ai.proactive import reconcile_answers
    from garmin_ai.telegram import process_message, save_update

    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    insight = Insight(
        category="trend",
        statement="Synthetic insight",
        evidence={},
        sample_size=1,
        effect_size=None,
        status="accepted",
        dedup_key="trend:synthetic:pause",
        generated_at=NOW,
    )
    db.add(insight)
    episode = create_event(
        db,
        EventInput(start=NOW - timedelta(hours=3), payload={"type": "migraine", "severity": 5}),
        actor="test",
    )
    question = PendingQuestion(
        kind="migraine",
        text="Synthetic migraine follow-up",
        evidence={},
        priority=1,
        earliest_send_at=NOW,
        expires_at=NOW + timedelta(days=1),
        status="pending",
        event_id=episode.id,
        dedup_key="migraine:synthetic:pause",
    )
    db.add(question)
    db.flush()
    db.add(
        AppState(
            key="insight:last:synthetic",
            value={"at": NOW.isoformat(), "reservation": str(insight.id)},
        )
    )
    db.commit()

    def control(update_id, command):
        save_update(
            db,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(NOW.timestamp()) + update_id,
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": command,
                },
            },
            42,
        )
        db.commit()
        return process_message(db_engine, None, Settings(telegram_user_id=42), update_id)

    assert "отключены" in control(7001, "/pause")
    db.expire_all()
    db.refresh(row)
    db.refresh(insight)
    db.refresh(question)
    assert row.state == DeliveryState.CANCELLED.value
    assert insight.status == "cancelled"
    assert insight.evidence["cancel_reason"] == "owner_pause"
    assert question.status == "cancelled"
    assert question.evidence["cancel_reason"] == "owner_pause"
    reconcile_answers(db, NOW + timedelta(hours=1))
    assert question.status == "cancelled"
    assert db.get(AppState, "insight:last:synthetic") is None
    assert claim_due_initiative(db, NOW) is None
    assert queue_due_checkin(db, instance.id, NOW) is None

    assert "разрешены" in control(7002, "/resume")
    reconcile_answers(db, NOW + timedelta(hours=1))
    assert question.status == "cancelled"
    assert claim_due_initiative(db, NOW) is None
    assert queue_due_checkin(db, instance.id, NOW) is row
    assert row.state == DeliveryState.CANCELLED.value


def test_delivery_fence_serializes_pause_policy_change(db_engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event as ThreadEvent

    from sqlalchemy import text

    from garmin_ai.runtime import initiative_delivery_fence

    started = ThreadEvent()

    def change_policy():
        with db_engine.begin() as connection:
            started.set()
            connection.execute(text("SELECT pg_advisory_xact_lock(72104621)"))
            return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        with initiative_delivery_fence(db_engine):
            future = executor.submit(change_policy)
            assert started.wait(timeout=5)
            assert not future.done()
        assert future.result(timeout=5)


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


def test_overnight_quiet_carry_keeps_scheduled_day_and_expires_after_morning(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(22, 0),
        quiet_end=time(8, 0),
    )
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    morning = datetime(2026, 9, 21, 8, tzinfo=UTC)

    row = queue_due_checkin(db, instance.id, due)

    assert row.intent["scheduled_day"] == "2026-09-20"
    assert row.intent["logical_notification_id"] == row.dedup_key
    assert row.next_attempt_at == morning
    assert datetime.fromisoformat(row.intent["expires_at"]) > morning
    assert claim_due_initiative(db, morning).outbox_message_id == row.id
    from garmin_ai.proactive import notification_count

    assert notification_count(db, Settings(timezone="UTC"), morning) == 1
    assert queue_due_checkin(db, instance.id, morning) is row


def test_delivered_carry_counts_on_actual_delivery_day_without_retry_date(db):
    from garmin_ai.proactive import notification_count

    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(22, 0),
        quiet_end=time(8, 0),
    )
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    morning = datetime(2026, 9, 21, 8, tzinfo=UTC)
    row = queue_due_checkin(db, instance.id, due)
    lease = claim_due_initiative(db, morning)
    assert lease is not None
    record_delivery_receipt(
        db,
        row.id,
        DeliveryReceipt(intent_id=row.id, state=DeliveryState.DELIVERED, observed_at=morning),
        lease_token=lease.lease_token,
    )
    row.next_attempt_at = None
    db.flush()

    assert notification_count(db, Settings(timezone="UTC"), morning) == 1


def test_later_delivery_receipt_does_not_count_initiative_again_next_day(db):
    from garmin_ai.proactive import notification_count

    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(0, 0),
        quiet_end=time(0, 0),
    )
    sent = datetime(2026, 9, 20, 23, tzinfo=UTC)
    next_day = datetime(2026, 9, 21, 8, tzinfo=UTC)
    row = queue_due_checkin(db, instance.id, sent)
    lease = claim_due_initiative(db, sent)
    assert lease is not None
    record_delivery_receipt(
        db,
        row.id,
        DeliveryReceipt(intent_id=row.id, state=DeliveryState.PROVIDER_ACCEPTED, observed_at=sent),
        lease_token=lease.lease_token,
    )
    record_delivery_receipt(
        db,
        row.id,
        DeliveryReceipt(intent_id=row.id, state=DeliveryState.DELIVERED, observed_at=next_day),
    )
    row.next_attempt_at = None
    db.flush()

    assert notification_count(db, Settings(timezone="UTC"), next_day) == 0


def test_accepted_carry_counts_even_if_outbox_later_fails(db):
    from garmin_ai.proactive import notification_count

    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(22, 0),
        quiet_end=time(8, 0),
    )
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    morning = datetime(2026, 9, 21, 8, tzinfo=UTC)
    row = queue_due_checkin(db, instance.id, due)
    lease = claim_due_initiative(db, morning)
    assert lease is not None
    record_delivery_receipt(
        db,
        row.id,
        DeliveryReceipt(
            intent_id=row.id, state=DeliveryState.PROVIDER_ACCEPTED, observed_at=morning
        ),
        lease_token=lease.lease_token,
    )
    row.state = DeliveryState.FAILED.value
    row.next_attempt_at = None
    db.flush()

    assert notification_count(db, Settings(timezone="UTC"), morning) == 1


def test_overnight_quiet_skips_when_quiet_end_exceeds_carry_window(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(22, 0),
        quiet_end=time(12, 0),
    )

    assert queue_due_checkin(db, instance.id, datetime(2026, 9, 20, 23, tzinfo=UTC)) is None
    assert db.scalar(select(OutboxMessage)) is None
    skipped = db.get(AppState, f"initiative:skip:{instance.id}:2026-09-20")
    assert skipped.value == {
        "reason": "defer_exceeds_carry_window",
        "policy_reason": "quiet_hours",
        "scheduled_day": "2026-09-20",
    }


def test_snooze_beyond_carry_records_skip_without_queuing(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        snoozed_until=datetime(2026, 9, 21, 12, tzinfo=UTC),
    )
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)

    assert queue_due_checkin(db, instance.id, due) is None
    assert db.scalar(select(OutboxMessage)) is None
    skipped = db.get(AppState, f"initiative:skip:{instance.id}:2026-09-20")
    assert skipped.value == {
        "reason": "defer_exceeds_carry_window",
        "policy_reason": "snoozed",
        "scheduled_day": "2026-09-20",
    }


def test_queued_reminder_recovers_after_normal_day_end(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(0, 0),
        quiet_end=time(0, 0),
    )
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    restarted = datetime(2026, 9, 21, 1, tzinfo=UTC)
    row = queue_due_checkin(db, instance.id, due)
    assert datetime.fromisoformat(row.intent["expires_at"]) == datetime(2026, 9, 21, tzinfo=UTC)

    lease = claim_due_initiative(db, restarted)

    assert lease is not None
    assert lease.outbox_message_id == row.id
    assert datetime.fromisoformat(row.intent["expires_at"]) > restarted


def test_legacy_fallback_reminder_recovers_its_scheduled_day_after_midnight(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(0, 0),
        quiet_end=time(0, 0),
    )
    fallback_conversation(db, instance.fallback_channels[0])
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    restarted = datetime(2026, 9, 21, 1, tzinfo=UTC)
    primary = queue_due_checkin(db, instance.id, due)
    primary.state = DeliveryState.FAILED.value
    fallback = reroute_failed(db, primary, now=due)
    assert fallback is not None and fallback.dedup_key.endswith(":2026-09-20:fallback:1")
    fallback.intent = {
        key: value
        for key, value in fallback.intent.items()
        if key not in {"scheduled_day", "logical_notification_id"}
    }
    assert datetime.fromisoformat(fallback.intent["expires_at"]) < restarted

    revalidate_before_send(db, fallback, restarted)

    assert fallback.state == DeliveryState.QUEUED.value
    assert datetime.fromisoformat(fallback.intent["expires_at"]) > restarted


def test_overnight_fallback_reserves_delivery_day_budget_until_carry_ends(db):
    from garmin_ai.proactive import notification_count

    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="schedule", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(0, 0),
        quiet_end=time(0, 0),
    )
    fallback_conversation(db, instance.fallback_channels[0])
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    morning = due + timedelta(hours=9)
    primary = queue_due_checkin(db, instance.id, due)
    primary.state = DeliveryState.FAILED.value
    fallback = reroute_failed(db, primary, now=due)
    assert fallback is not None and fallback.next_attempt_at is None
    primary.created_at = due
    fallback.created_at = due
    db.flush()

    assert notification_count(db, Settings(timezone="UTC"), morning) == 1
    assert notification_count(db, Settings(timezone="UTC"), due + timedelta(hours=13)) == 0


def test_legacy_schedule_without_expiry_recovers_within_carry(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="schedule", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(0, 0),
        quiet_end=time(0, 0),
    )
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    restarted = due + timedelta(hours=2)
    row = queue_due_checkin(db, instance.id, due)
    row.intent = {
        key: value
        for key, value in row.intent.items()
        if key not in {"scheduled_day", "logical_notification_id", "expires_at"}
    }

    revalidate_before_send(db, row, restarted)

    assert row.state == DeliveryState.QUEUED.value
    assert datetime.fromisoformat(row.intent["expires_at"]) > restarted


@pytest.mark.parametrize("retry_hours,expected_state", [(2, "queued"), (13, "expired")])
def test_adapter_retry_respects_carry_window_and_records_skip(db, retry_hours, expected_state):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="schedule", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(0, 0),
        quiet_end=time(0, 0),
    )
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    row = queue_due_checkin(db, instance.id, due)
    lease = claim_due_initiative(db, due)
    assert lease is not None
    finish_initiative_attempt(
        db,
        lease,
        DeliveryAttempt(
            intent_id=row.id,
            state=DeliveryState.QUEUED,
            retry_after=due + timedelta(hours=retry_hours),
        ),
        due,
    )

    assert row.state == expected_state
    assert row.lease_token is None and row.lease_until is None
    if expected_state == "queued":
        assert row.next_attempt_at == due + timedelta(hours=2)
        assert datetime.fromisoformat(row.intent["expires_at"]) > row.next_attempt_at
    else:
        assert row.next_attempt_at is None
        skipped = db.get(AppState, f"initiative:skip:{instance.id}:2026-09-20")
        assert skipped.value == {
            "reason": "defer_exceeds_carry_window",
            "policy_reason": "adapter_retry",
            "scheduled_day": "2026-09-20",
        }


def test_new_snooze_can_defer_existing_reminder_past_midnight_within_carry(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(0, 0),
        quiet_end=time(0, 0),
    )
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    before_midnight = due + timedelta(minutes=30)
    after_midnight = due + timedelta(hours=1, minutes=30)
    row = queue_due_checkin(db, instance.id, due)
    save_rule(db, instance.model_copy(update={"snoozed_until": after_midnight}))

    assert claim_due_initiative(db, before_midnight) is None
    assert row.state == DeliveryState.QUEUED.value
    assert row.next_attempt_at == after_midnight
    assert datetime.fromisoformat(row.intent["expires_at"]) > after_midnight
    lease = claim_due_initiative(db, after_midnight)
    assert lease is not None and lease.outbox_message_id == row.id


def test_carry_expires_immediately_when_new_snooze_exceeds_its_bound(db):
    from garmin_ai.proactive import notification_count

    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
        quiet_start=time(22, 0),
        quiet_end=time(8, 0),
    )
    due = datetime(2026, 9, 20, 23, tzinfo=UTC)
    morning = datetime(2026, 9, 21, 8, tzinfo=UTC)
    row = queue_due_checkin(db, instance.id, due)
    assert row.next_attempt_at == morning
    save_rule(db, instance.model_copy(update={"snoozed_until": morning + timedelta(hours=4)}))

    assert claim_due_initiative(db, morning) is None
    assert row.state == DeliveryState.EXPIRED.value
    assert row.next_attempt_at is None
    assert notification_count(db, Settings(timezone="UTC"), morning) == 0
    skipped = db.get(AppState, f"initiative:skip:{instance.id}:2026-09-20")
    assert skipped.value == {
        "reason": "defer_exceeds_carry_window",
        "policy_reason": "snoozed",
        "scheduled_day": "2026-09-20",
    }


def test_recent_previous_day_checkin_is_recovered_after_midnight(db):
    instance = configured_rule(
        db,
        rule=RuleDefinition(kind="missing_entry", prompt="Check in", local_time=time(23, 0)),
    )
    restarted_at = datetime(2026, 9, 21, 1, tzinfo=UTC)

    row = queue_due_checkin(db, instance.id, restarted_at)

    assert row is not None
    assert row.dedup_key.endswith(":2026-09-20")
    assert datetime.fromisoformat(row.intent["expires_at"]) > restarted_at


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


def test_tracker_checkin_reserves_budget_under_exclusive_policy_lock(db, db_engine):
    instance = configured_rule(db, daily_budget=1)
    assert queue_due_checkin(db, instance.id, NOW) is not None

    with db_engine.connect() as concurrent:
        assert concurrent.scalar(text("SELECT pg_try_advisory_xact_lock_shared(72104621)")) is False


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
    db.add(
        AppState(
            key="conversation:pending:restricted-test:primary",
            value={
                "created_at": NOW.isoformat(),
                "channel_instance_id": "restricted-test:primary",
            },
        )
    )
    db.flush()

    assert claim_due_initiative(db, NOW) is None
    assert row.state == DeliveryState.QUEUED.value


def test_other_channel_pending_form_does_not_defer_neutral_initiative(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    db.add(AppState(key="conversation:pending", value={"created_at": NOW.isoformat()}))
    db.flush()

    lease = claim_due_initiative(db, NOW)
    assert lease is not None and lease.outbox_message_id == row.id


def test_claim_searches_past_twenty_initiatives_blocked_by_another_channel(db):
    instance = configured_rule(db)
    first = queue_due_checkin(db, instance.id, NOW)
    first.created_at = NOW - timedelta(days=1)
    template = OutboundIntent.model_validate(first.intent)
    alternate_conversation_id = uuid4()
    db.add(
        Conversation(
            id=alternate_conversation_id,
            owner_id=owner(db).id,
            channel="telegram",
            channel_instance_id="primary",
            external_conversation_id="synthetic-alternate",
            memory_epoch=uuid4(),
            state={},
        )
    )
    for index in range(1, 21):
        channel = (
            ChannelInstanceRef(channel="restricted-test", instance_id="primary")
            if index < 20
            else ChannelInstanceRef(channel="telegram", instance_id="primary")
        )
        intent = template.model_copy(
            update={
                "intent_id": uuid4(),
                "channel_instance": channel,
                "conversation_id": (
                    alternate_conversation_id if index == 20 else instance.conversation_id
                ),
            }
        )
        row = queue_intent(db, intent, operation_id=uuid4(), dedup_key=f"synthetic:{index}")
        row.created_at = NOW if index == 20 else NOW - timedelta(days=1) + timedelta(seconds=index)
    db.add(
        AppState(
            key="conversation:pending:restricted-test:primary",
            value={"created_at": NOW.isoformat(), "channel_instance_id": "restricted-test:primary"},
        )
    )
    db.flush()

    lease = claim_due_initiative(db, NOW)
    assert lease is not None
    assert lease.intent.channel_instance == ChannelInstanceRef(
        channel="telegram", instance_id="primary"
    )


def test_claim_skips_unconfigured_channel_instances(db):
    instance = configured_rule(db)
    primary = queue_due_checkin(db, instance.id, NOW)
    assert (
        claim_due_initiative(db, NOW, supported_destinations=frozenset({"telegram:primary"}))
        is None
    )
    assert primary.state == DeliveryState.QUEUED.value
    lease = claim_due_initiative(
        db, NOW, supported_destinations=frozenset({"restricted-test:primary"})
    )
    assert lease is not None and lease.outbox_message_id == primary.id


def test_claimed_initiative_is_cancelled_if_channel_consent_changes_before_send(db):
    instance = configured_rule(db, privacy="sensitive")
    version = db.get(EventDefinitionVersion, instance.definition_version_id)
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
    row = queue_due_checkin(db, instance.id, NOW)
    lease = claim_due_initiative(db, NOW)
    assert lease is not None and row.state == DeliveryState.SENDING.value

    revoke_tracker_share(
        db,
        version.definition_id,
        "channel",
        "restricted-test:primary",
        authorized=True,
    )
    revalidate_before_send(db, row, NOW)
    assert row.state == DeliveryState.CANCELLED.value


def test_channel_fallback_requires_known_failure_and_never_duplicates_uncertain(db):
    instance = configured_rule(db)
    target = fallback_conversation(db, instance.fallback_channels[0])
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
    assert fallback.conversation_id == target.id
    assert fallback.intent["conversation_id"] == str(target.id)
    assert fallback.operation_id == row.operation_id
    assert fallback.intent["logical_notification_id"] == row.intent["logical_notification_id"]


def test_sensitive_fallback_without_channel_consent_keeps_known_failure(db):
    instance = configured_rule(db, privacy="sensitive")
    version = db.get(EventDefinitionVersion, instance.definition_version_id)
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
    row = queue_due_checkin(db, instance.id, NOW)
    row.state = DeliveryState.FAILED.value

    assert reroute_failed(db, row, now=NOW) is None
    assert row.state == DeliveryState.FAILED.value
    assert db.scalar(select(OutboxMessage).where(OutboxMessage.id != row.id)) is None


def test_failed_primary_does_not_consume_fallback_budget(db):
    instance = configured_rule(db, daily_budget=1)
    fallback_conversation(db, instance.fallback_channels[0])
    primary = queue_due_checkin(db, instance.id, NOW)
    primary.state = DeliveryState.FAILED.value
    fallback = reroute_failed(db, primary, now=NOW)

    assert fallback is not None
    assert claim_due_initiative(db, NOW).outbox_message_id == fallback.id


def test_channel_fallback_advances_once_through_the_entire_chain(db):
    instance = configured_rule(
        db,
        fallback_channels=[
            ChannelInstanceRef(channel="telegram", instance_id="first"),
            ChannelInstanceRef(channel="telegram", instance_id="second"),
        ],
    )
    first_target = fallback_conversation(db, instance.fallback_channels[0])
    second_target = fallback_conversation(db, instance.fallback_channels[1])
    row = queue_due_checkin(db, instance.id, NOW)
    row.state = DeliveryState.FAILED.value

    first = reroute_failed(db, row, now=NOW)
    first.state = DeliveryState.FAILED.value
    second = reroute_failed(db, first, now=NOW)
    second.state = DeliveryState.FAILED.value

    assert first.intent["channel_instance"]["instance_id"] == "first"
    assert second.intent["channel_instance"]["instance_id"] == "second"
    assert first.conversation_id == first_target.id
    assert second.conversation_id == second_target.id
    assert reroute_failed(db, second, now=NOW) is None


def test_fallback_requires_one_authenticated_target_conversation(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    row.state = DeliveryState.FAILED.value
    assert reroute_failed(db, row, now=NOW) is None

    fallback_conversation(db, instance.fallback_channels[0])
    fallback_conversation(db, instance.fallback_channels[0])
    assert reroute_failed(db, row, now=NOW) is None


def test_fallback_rechecks_target_consent_and_shared_text_capability(db):
    instance = configured_rule(db, privacy="sensitive")
    version = db.get(EventDefinitionVersion, instance.definition_version_id)
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
    fallback_conversation(db, instance.fallback_channels[0])
    row = queue_due_checkin(db, instance.id, NOW)
    row.state = DeliveryState.FAILED.value
    assert reroute_failed(db, row, now=NOW) is None

    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=version.definition_id,
            destination_kind="channel",
            destination_instance_id="telegram:primary",
            categories={"schema", "facts"},
            granted_at=NOW,
        ),
        authorized=True,
    )
    row.intent = {**row.intent, "preferred_medium": "voice"}
    assert reroute_failed(db, row, now=NOW) is None
    row.intent = {**row.intent, "preferred_medium": "text"}
    assert reroute_failed(db, row, now=NOW) is not None


def test_legacy_fallback_with_primary_conversation_is_cancelled_before_send(db):
    instance = configured_rule(db)
    row = queue_due_checkin(db, instance.id, NOW)
    row.intent = {
        **row.intent,
        "channel_instance": instance.fallback_channels[0].model_dump(),
    }

    revalidate_before_send(db, row, NOW)

    assert row.state == DeliveryState.CANCELLED.value


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
