"""Idempotent owner onboarding without integration or secret requirements."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from sqlalchemy import select

from garmin_ai.accounts import owner
from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.events import StrictModel
from garmin_ai.i18n import SUPPORTED_LOCALES, translate
from garmin_ai.integrations import integration_statuses
from garmin_ai.models import AppState, EventDefinition, TrackerConfig
from garmin_ai.normalize import upsert
from garmin_ai.scenario_packs import (
    PACKS,
    PackSelection,
    configure_scenario_pack,
    ensure_scenario_packs,
    list_scenario_packs,
)
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

ONBOARDING_KEY = "preferences:onboarding"


def source_instance_selected(session, instance_id: str) -> bool:
    """Honor onboarding selection once the owner has completed onboarding."""

    saved = session.get(AppState, ONBOARDING_KEY, populate_existing=True)
    if saved is None:
        return True
    return instance_id in set(saved.value.get("source_instance_ids", []))


class OnboardingPlan(StrictModel):
    locale: Literal["en", "ru"]
    timezone: str
    units: Literal["metric", "imperial"]
    selected_packs: set[str] = Field(default_factory=set, max_length=20)
    reminder_packs: set[str] = Field(default_factory=set, max_length=20)
    trackers: list[TrackerSetupDraft] = Field(default_factory=list, max_length=32)
    source_instance_ids: set[str] = Field(default_factory=set, max_length=20)
    channel: ChannelInstanceRef | None = None
    model_categories: set[Literal["health", "diary", "audio"]] = Field(default_factory=set)

    @model_validator(mode="after")
    def valid_choices(self):
        ZoneInfo(self.timezone)
        unknown = (self.selected_packs | self.reminder_packs) - set(PACKS)
        if unknown:
            raise ValueError("Unknown scenario pack")
        if not self.reminder_packs <= self.selected_packs:
            raise ValueError("Reminders require a selected pack")
        if len({tracker.key for tracker in self.trackers}) != len(self.trackers):
            raise ValueError("Tracker manifest contains duplicate keys")
        return self


def import_tracker_manifest(payload) -> list[TrackerSetupDraft]:
    """Data-only manifest: strict models reject secrets, hooks, URLs, and extra fields."""

    if not isinstance(payload, dict) or set(payload) != {"trackers"}:
        raise ValueError("Tracker manifest may contain only trackers")
    if not isinstance(payload["trackers"], list) or len(payload["trackers"]) > 32:
        raise ValueError("Tracker manifest is invalid or too large")
    return [TrackerSetupDraft.model_validate(item) for item in payload["trackers"]]


def apply_onboarding(session, plan: OnboardingPlan):
    plan = OnboardingPlan.model_validate(plan)
    person = owner(session)
    person.locale, person.timezone, person.units = plan.locale, plan.timezone, plan.units

    packs = ensure_scenario_packs(session, legacy_install=False)
    for key, row in packs.items():
        selected = key in plan.selected_packs
        configure_scenario_pack(
            session,
            key,
            PackSelection(
                revision=row.revision,
                tracking_enabled=selected,
                collection_enabled=selected,
                reminders_enabled=key in plan.reminder_packs,
                visible=selected,
                llm_enabled=selected and "diary" in plan.model_categories,
                outcome_goal=None,
            ),
        )

    created = []
    existing_keys = set(
        session.scalars(
            select(EventDefinition.key)
            .join(TrackerConfig, TrackerConfig.definition_id == EventDefinition.id)
            .where(EventDefinition.owner_id == person.id)
        )
    )
    for draft in plan.trackers:
        key = "user." + draft.key
        if key in existing_keys:
            continue
        preview = preview_tracker(session, draft)
        created.append(
            confirm_tracker(
                session,
                TrackerConfirmation(
                    draft=draft,
                    confirmation_token=preview["confirmation_token"],
                ),
                actor="onboarding",
            )
        )
        existing_keys.add(key)

    previous = session.get(AppState, ONBOARDING_KEY)
    revision = (previous.value.get("revision", 0) if previous else 0) + 1
    value = {
        "revision": revision,
        "locale": plan.locale,
        "timezone": plan.timezone,
        "units": plan.units,
        "selected_packs": sorted(plan.selected_packs),
        "source_instance_ids": sorted(plan.source_instance_ids),
        "channel": plan.channel.model_dump(mode="json") if plan.channel else None,
        "model_categories": sorted(plan.model_categories),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    upsert(session, AppState, {"key": ONBOARDING_KEY, "value": value}, ["key"])
    session.flush()
    return {
        "complete": True,
        "message": translate("onboarding.complete", plan.locale),
        "preferences": value,
        "created_trackers": created,
        "packs": list_scenario_packs(session),
    }


def onboarding_status(session, settings):
    person = owner(session)
    saved = session.get(AppState, ONBOARDING_KEY)
    statuses = [row.model_dump(mode="json") for row in integration_statuses(settings)]
    return {
        "complete": saved is not None,
        "locale": person.locale,
        "timezone": person.timezone,
        "units": person.units,
        "supported_locales": sorted(SUPPORTED_LOCALES),
        "packs": list_scenario_packs(session),
        "integrations": statuses,
        "can_finish_without_sources": True,
        "message": (
            translate("onboarding.complete", person.locale)
            if saved
            else translate("onboarding.no_sources", person.locale)
        ),
    }
