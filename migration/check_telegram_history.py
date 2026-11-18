"""Record the pinned Telegram diary history on a disposable shared fixture."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.definitions import CustomEntryInput, create_custom_event
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState
from garmin_ai.share_policy import TrackerShareConsent, grant_tracker_share
from garmin_ai.telegram_history import history_page, selected_action
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)


def seed_sensitive_trackers(session, now):
    for key in ("shared_history", "restricted_history"):
        draft = TrackerSetupDraft(
            key=key,
            name=key,
            locale="en",
            privacy="sensitive",
            fields=[
                TrackerFieldDraft(
                    key="description",
                    label="Description",
                    kind="text",
                    max_length=100,
                )
            ],
        )
        preview = preview_tracker(session, draft)
        created = confirm_tracker(
            session,
            TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
            actor="synthetic-fixture",
        )
        create_custom_event(
            session,
            CustomEntryInput(
                definition_key=f"user.{key}",
                start=now,
                timezone="UTC",
                values={"description": f"Synthetic {key}"},
            ),
            actor="synthetic-fixture",
        )
        if key == "shared_history":
            grant_tracker_share(
                session,
                TrackerShareConsent(
                    definition_id=created["tracker"]["definition_id"],
                    destination_kind="channel",
                    destination_instance_id="telegram:primary",
                    categories={"schema", "facts"},
                    granted_at=now,
                ),
                authorized=True,
            )


def capture(destination):
    engine = isolated_engine()
    now = datetime.now(UTC).replace(microsecond=0)
    recent = [
        {
            "type": "caffeine",
            "beverage": "synthetic coffee",
            "servings": 1.5,
            "dose_basis": "per_serving",
            "dose_provenance": "estimated",
            "caffeine_mg_min": 16.666666,
            "caffeine_mg_max": 38,
        },
        {"type": "migraine", "severity": 4},
        {"type": "wellbeing_observation", "energy": 0, "notes": "fictional"},
        {"type": "headache_observation", "headache": "yes", "migraine": "unknown"},
        {"type": "medication", "name": "synthetic medication", "dose": 20, "unit": "mg"},
        {"type": "caffeine_log_complete", "description": "fictional full day"},
    ]
    older = [{"type": "note", "description": f"Synthetic item {index}"} for index in range(7)]

    def capture_page(session, text):
        keyboard = session.info["reply_keyboard"]["inline_keyboard"]
        return {"text": text, "buttons": [button["text"] for row in keyboard for button in row]}

    try:
        with Session(engine) as session:
            session.info["channel_instance"] = ChannelInstanceRef(
                channel="telegram", instance_id="primary"
            )
            for index, payload in enumerate(recent + older):
                start = now - timedelta(minutes=index + 1)
                end = (
                    start + timedelta(minutes=1)
                    if payload["type"] in {"headache_observation", "caffeine_log_complete"}
                    else None
                )
                create_event(
                    session,
                    EventInput(
                        start=start,
                        end=end,
                        timezone="Europe/Bratislava",
                        payload=payload,
                    ),
                    actor="synthetic-fixture",
                )
            seed_sensitive_trackers(session, now)
            session.commit()
            first = capture_page(session, history_page(session, now))
            assert "Synthetic shared_history" in first["text"]
            assert "Synthetic restricted_history" not in first["text"]
            keyboard = session.info["reply_keyboard"]["inline_keyboard"]
            next_page = keyboard[-1][-1]["callback_data"]
            session.flush()
            second = capture_page(session, selected_action(session, next_page, now, "owner"))
            open_only = capture_page(session, history_page(session, now, open_only=True))
            open_actions = session.info["reply_keyboard"]["inline_keyboard"][0]
            session.flush()
            chosen = capture_page(
                session, selected_action(session, open_actions[0]["callback_data"], now, "owner")
            )
            pending = session.get(AppState, "conversation:pending")
            chosen["event_id"] = pending.value["event_ids"][0]
            chosen["selection_revision"] = pending.value["selection_revision"]
            session.flush()
            removed = selected_action(session, open_actions[1]["callback_data"], now, "owner")
            session.flush()
            assert session.get(AppState, "conversation:pending") is None
            session.rollback()
        destination.write_text(
            json.dumps([first, second, open_only, chosen, {"text": removed}], ensure_ascii=False)
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    capture(parser.parse_args().destination)
