from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from garmin_ai.accounts import apply_instance_settings
from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.i18n import translate
from garmin_ai.models import Event, EventDefinition, TrackerConfig
from garmin_ai.onboarding import (
    OnboardingPlan,
    apply_onboarding,
    import_tracker_manifest,
    onboarding_status,
    source_instance_selected,
)
from garmin_ai.scenario_packs import pack_enabled
from garmin_ai.tracker_forms import TrackerFieldDraft, TrackerSetupDraft, available_actions


def focus_tracker(locale="en"):
    return TrackerSetupDraft(
        key="focus",
        name="Focus" if locale == "en" else "Фокус",
        locale=locale,
        topology="point",
        fields=[
            TrackerFieldDraft(
                key="quality",
                label="Quality" if locale == "en" else "Качество",
                kind="scale",
                minimum=1,
                maximum=5,
            )
        ],
        shortcut="Log focus" if locale == "en" else "Записать фокус",
    )


def plan(**changes):
    values = dict(
        locale="en",
        timezone="UTC",
        units="metric",
        selected_packs={"general_diary"},
        reminder_packs=set(),
        trackers=[focus_tracker()],
        source_instance_ids=set(),
        channel=None,
        model_categories=set(),
    )
    values.update(changes)
    return OnboardingPlan(**values)


def test_setup_without_garmin_or_migraine_is_complete_and_capability_honest(db):
    result = apply_onboarding(db, plan())
    status = onboarding_status(db, __import__("garmin_ai.config", fromlist=["Settings"]).Settings())

    assert result["complete"] and status["complete"]
    assert status["can_finish_without_sources"]
    assert not pack_enabled(db, "migraine")
    assert not pack_enabled(db, "migraine", "reminders")


def test_language_change_preserves_ids_payload_scale_history_and_existing_tracker(db):
    apply_onboarding(db, plan())
    action_before = available_actions(db)[0]
    event = create_event(
        db,
        EventInput(
            start=datetime(2026, 9, 20, 18, tzinfo=UTC),
            timezone="UTC",
            source="manual",
            payload={"type": "note", "description": "kept"},
        ),
        actor="test",
    )
    db.flush()

    apply_onboarding(
        db,
        plan(
            locale="ru",
            timezone="Europe/Bratislava",
            units="imperial",
            trackers=[focus_tracker("ru")],
        ),
    )
    action_after = available_actions(db, locale="ru")[0]

    assert action_after.definition_version_id == action_before.definition_version_id
    assert action_after.id == action_before.id
    assert db.get(Event, event.id).payload["type"] == "note"
    assert db.get(Event, event.id).payload["description"] == "kept"
    assert db.scalar(select(func.count()).select_from(TrackerConfig)) == 1
    assert translate("onboarding.complete", "ru") == "Настройка завершена."


def test_repeated_setup_changes_only_preferences_and_keeps_keys_and_consent_data(db):
    first = apply_onboarding(
        db,
        plan(
            channel=ChannelInstanceRef(channel="restricted-test", instance_id="primary"),
            model_categories={"diary"},
        ),
    )
    second = apply_onboarding(db, plan(source_instance_ids={"source:garmin:later"}))

    assert second["preferences"]["revision"] == first["preferences"]["revision"] + 1
    assert second["preferences"]["source_instance_ids"] == ["source:garmin:later"]
    assert (
        db.scalar(
            select(func.count())
            .select_from(EventDefinition)
            .where(EventDefinition.namespace == "user")
        )
        == 1
    )


def test_process_restart_preserves_completed_onboarding_preferences(db):
    apply_onboarding(db, plan(locale="ru", timezone="Europe/Bratislava", units="imperial"))

    person = apply_instance_settings(db, Settings(locale="en", timezone="UTC", units="metric"))

    assert (person.locale, person.timezone, person.units) == (
        "ru",
        "Europe/Bratislava",
        "imperial",
    )


def test_empty_onboarding_source_selection_retires_legacy_garmin_jobs(db, db_engine):
    from garmin_ai.jobs import enqueue
    from garmin_ai.models import Job
    from garmin_ai.runtime import claim_ready_job

    apply_onboarding(db, plan(source_instance_ids=set()))
    identity = enqueue(
        db,
        "garmin_activities",
        {},
        "synthetic-onboarding-disabled-source",
        datetime.now(UTC),
    )
    db.commit()

    claimed = claim_ready_job(
        db_engine,
        ["garmin_activities"],
        backups_enabled=False,
        has_bot=False,
        source_instance_id="source:garmin:primary",
    )

    assert claimed is None
    db.expire_all()
    assert not source_instance_selected(db, "source:garmin:primary")
    assert db.get(Job, identity).last_error == "IntegrationDisabled"


def test_data_only_manifest_rejects_secrets_and_hooks():
    assert import_tracker_manifest({"trackers": [focus_tracker().model_dump(mode="json")]})
    with pytest.raises((ValidationError, ValueError)):
        import_tracker_manifest(
            {
                "trackers": [
                    {
                        **focus_tracker().model_dump(mode="json"),
                        "webhook": "https://example.invalid/steal",
                    }
                ]
            }
        )
    with pytest.raises(ValueError):
        import_tracker_manifest({"trackers": [], "token": "secret"})
