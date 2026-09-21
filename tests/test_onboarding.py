from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from garmin_ai.accounts import apply_instance_settings, effective_owner_settings
from garmin_ai.api import create_app
from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.i18n import translate
from garmin_ai.models import Event, EventDefinition, Job, ModuleConfig, TrackerConfig
from garmin_ai.onboarding import (
    OnboardingPlan,
    apply_onboarding,
    import_tracker_manifest,
    onboarding_status,
)
from garmin_ai.scenario_packs import pack_enabled
from garmin_ai.sync import schedule_sync
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


def test_unknown_timezone_is_a_validation_error():
    with pytest.raises(ValidationError, match="Unknown timezone"):
        plan(timezone="Mars/Olympus_Mons")


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


def test_onboarding_model_categories_and_existing_outcome_goal_are_preserved(db):
    apply_onboarding(db, plan(model_categories={"diary"}))
    pack = db.scalar(select(ModuleConfig).where(ModuleConfig.pack_key == "general_diary"))
    assert not pack.llm_enabled
    pack.outcome_goal = "Keep a synthetic weekly routine"
    db.flush()

    apply_onboarding(db, plan(model_categories={"health", "diary"}))

    assert pack.llm_enabled
    assert pack.outcome_goal == "Keep a synthetic weekly routine"


def test_onboarding_reports_worker_declared_integrations_without_api_credentials(db):
    settings = Settings(
        integrations=[
            {"id": "source:garmin:primary", "kind": "source", "provider": "garmin"},
            {
                "id": "channel:telegram:primary",
                "kind": "channel",
                "provider": "telegram",
            },
        ]
    )

    statuses = onboarding_status(db, settings)["integrations"]

    assert {row["instance_id"] for row in statuses if row["available"]} == {
        "source:garmin:primary",
        "channel:telegram:primary",
    }


def test_process_restart_preserves_completed_onboarding_preferences(db):
    apply_onboarding(db, plan(locale="ru", timezone="Europe/Bratislava", units="imperial"))

    person = apply_instance_settings(db, Settings(locale="en", timezone="UTC", units="metric"))

    assert (person.locale, person.timezone, person.units) == (
        "ru",
        "Europe/Bratislava",
        "imperial",
    )


def test_saved_owner_timezone_drives_runtime_and_sync_calendar_dates(db):
    apply_onboarding(db, plan(locale="ru", timezone="Asia/Tokyo", units="imperial"))
    configured = Settings(locale="en", timezone="UTC", units="metric", backfill_days=0)

    effective = effective_owner_settings(db, configured)
    schedule_sync(db, configured, datetime(2026, 9, 20, 23, 30, tzinfo=UTC))
    jobs = db.scalars(select(Job)).all()

    assert (effective.locale, effective.timezone, effective.units) == (
        "ru",
        "Asia/Tokyo",
        "imperial",
    )
    assert db.info["timezone"] == "Asia/Tokyo"
    assert any(
        row.payload.get("endpoint") == "daily" and row.payload.get("key") == "2026-09-21"
        for row in jobs
    )


def test_deselected_onboarding_channel_rejects_webhook_traffic(db, db_engine):
    apply_onboarding(db, plan(channel=None))
    db.commit()
    secret = "synthetic-webhook-secret"
    client = TestClient(
        create_app(
            Settings(
                telegram_user_id=42,
                telegram_webhook_secret=secret,
            ),
            db_engine,
        )
    )

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        json={},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Telegram channel is disabled"


def test_selected_telegram_webhook_does_not_require_outbound_bot_token(db, db_engine):
    apply_onboarding(
        db,
        plan(channel=ChannelInstanceRef(channel="telegram", instance_id="primary")),
    )
    db.commit()
    secret = "synthetic-webhook-secret"
    client = TestClient(
        create_app(
            Settings(
                telegram_user_id=42,
                telegram_webhook_secret=secret,
            ),
            db_engine,
        )
    )

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        json={
            "update_id": 92,
            "message": {
                "message_id": 92,
                "date": 1_789_000_000,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "synthetic",
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": True}


def test_non_primary_telegram_instance_rejects_webhook_traffic(db_engine):
    secret = "synthetic-webhook-secret"
    client = TestClient(
        create_app(
            Settings(
                integrations=[
                    {
                        "id": "channel:telegram:secondary",
                        "kind": "channel",
                        "provider": "telegram",
                    }
                ],
                telegram_bot_token="synthetic-bot-token",
                telegram_user_id=42,
                telegram_webhook_secret=secret,
            ),
            db_engine,
        )
    )

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        json={},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Telegram channel is disabled"


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
