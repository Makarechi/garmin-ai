import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from garmin_ai.agent import Interpretation, apply_command, interpret
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState
from garmin_ai.telegram import deliver, handle_button
from garmin_ai.telegram_history import history_page, selected_action

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


class Provider:
    def __init__(self, result):
        self.result = result

    def structured(self, instruction, prompt, schema):
        return self.result


def test_paginate_to_old_record_and_edit_without_uuid(db):
    rows = [
        create_event(
            db,
            EventInput(
                timezone="UTC",
                start=NOW - timedelta(days=i),
                payload={"type": "note", "description": f"synthetic-{i}"},
            ),
            actor="owner",
        )
        for i in range(25)
    ]
    page = history_page(db, NOW)
    assert str(rows[0].id) not in page
    for _ in range(2):
        callback = db.info["reply_keyboard"]["inline_keyboard"][-1][-1]["callback_data"]
        page = selected_action(db, callback, NOW, "owner")
    assert "synthetic-24" in page and "synthetic-0" not in page
    callback = db.info["reply_keyboard"]["inline_keyboard"][4][0]["callback_data"]
    selected_action(db, callback, NOW, "owner")
    target = rows[24]
    command = Interpretation(
        intent="update",
        confidence=1,
        target_event_id=target.id,
        changed_fields=["payload.description"],
        events=[
            EventInput(
                timezone="UTC",
                start=target.start,
                payload={"type": "note", "description": "corrected"},
            )
        ],
    )
    result = interpret(db, Provider(command), "исправь заметку", Settings(timezone="UTC"), NOW)
    assert result.intent == "update"
    apply_command(db, result, text="исправь заметку", update_id=700, actor="owner", now=NOW)
    assert target.payload["description"] == "corrected"
    assert rows[0].payload["description"] == "synthetic-0"


@pytest.mark.parametrize("stale", ["expired", "revision"])
def test_old_delete_button_does_not_mutate_record(db, stale):
    row = create_event(
        db,
        EventInput(timezone="UTC", start=NOW, payload={"type": "note", "description": "synthetic"}),
        actor="owner",
    )
    history_page(db, NOW)
    callback = db.info["reply_keyboard"]["inline_keyboard"][0][1]["callback_data"]
    if stale == "revision":
        row.revision += 1
        db.flush()
    selected_action(
        db, callback, NOW + timedelta(minutes=16) if stale == "expired" else NOW, "owner"
    )
    assert not row.deleted


def test_open_migraine_selector_targets_only_chosen_episode(db):
    first = create_event(
        db,
        EventInput(timezone="UTC", start=NOW - timedelta(days=2), payload={"type": "migraine"}),
        actor="owner",
    )
    second = create_event(
        db,
        EventInput(timezone="UTC", start=NOW - timedelta(days=1), payload={"type": "migraine"}),
        actor="owner",
    )
    handle_button(db, "end", Settings(timezone="UTC"), "owner", 1, NOW)
    callback = db.info["reply_keyboard"]["inline_keyboard"][1][0]["callback_data"]
    selected_action(db, callback, NOW, "owner")
    pending = db.get(AppState, "conversation:pending", populate_existing=True).value
    assert pending["event_ids"] == [str(first.id)]
    assert pending["question"] == "Во сколько закончилась мигрень?"
    command = Interpretation(
        intent="close",
        confidence=1,
        target_event_id=first.id,
        events=[
            EventInput(timezone="UTC", start=first.start, end=NOW, payload={"type": "migraine"})
        ],
    )
    result = interpret(db, Provider(command), "сейчас", Settings(timezone="UTC"), NOW)
    assert result.intent == "close", result.clarification
    apply_command(db, result, text="сейчас", update_id=2, actor="owner", now=NOW)
    assert first.end == NOW and second.end is None


def test_new_meal_can_follow_optional_coffee_refinement(db):
    create_event(
        db,
        EventInput(
            start=NOW - timedelta(days=1),
            timezone="UTC",
            payload={"type": "meal", "description": "earlier synthetic meal"},
        ),
        actor="owner",
    )
    handle_button(db, "coffee", Settings(timezone="UTC"), "owner", 1, NOW)
    command = Interpretation(
        intent="log",
        confidence=1,
        events=[
            EventInput(
                timezone="UTC",
                start=NOW,
                payload={"type": "meal", "description": "synthetic lunch"},
            )
        ],
    )
    result = interpret(db, Provider(command), "пообедал", Settings(timezone="UTC"), NOW)
    assert result.intent == "log" and result._dismiss_refinement, result.clarification


def test_selected_revision_changed_before_followup_requires_new_selection(db):
    row = create_event(
        db,
        EventInput(timezone="UTC", start=NOW, payload={"type": "note", "description": "synthetic"}),
        actor="owner",
    )
    history_page(db, NOW)
    selected_action(
        db, db.info["reply_keyboard"]["inline_keyboard"][0][0]["callback_data"], NOW, "owner"
    )
    row.revision += 1
    db.flush()
    result = interpret(db, Provider(None), "исправь", Settings(timezone="UTC"), NOW)
    assert result.intent == "clarify"


def test_custom_keyboard_survives_json_and_delivery(db, db_engine):
    history_page(db, NOW)
    keyboard = db.info["reply_keyboard"]
    db.commit()
    calls = []

    class Bot:
        async def send_message(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=1)

    asyncio.run(deliver(Bot(), db_engine, 1, "history-synthetic", "History", keyboard=keyboard))
    assert calls[0]["reply_markup"].inline_keyboard[0][0].callback_data.startswith("h:")


def test_history_reply_keyboard_is_durable_and_replay_keeps_same_selectors(db, db_engine):
    from garmin_ai.telegram import process_message, save_update

    now = datetime.now(UTC)
    save_update(
        db,
        {
            "update_id": 999,
            "message": {
                "message_id": 999,
                "date": int(now.timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/history",
            },
        },
        42,
    )
    db.commit()
    first = process_message(db_engine, None, Settings(telegram_user_id=42), 999)
    saved = db.get(AppState, "telegram:reply:999", populate_existing=True).value
    keyboard = saved["keyboard"]
    assert keyboard["inline_keyboard"][-1][0]["callback_data"].startswith("h:")
    assert process_message(db_engine, None, Settings(telegram_user_id=42), 999) == first
    assert (
        db.get(AppState, "telegram:reply:999", populate_existing=True).value["keyboard"] == keyboard
    )


def test_delete_from_migraine_picker_clears_its_close_prompt(db):
    for hours in (2, 3):
        create_event(
            db,
            EventInput(
                start=NOW - timedelta(hours=hours), timezone="UTC", payload={"type": "migraine"}
            ),
            actor="owner",
        )
    handle_button(db, "end", Settings(timezone="UTC"), "owner", 101, NOW)
    callback = db.info["reply_keyboard"]["inline_keyboard"][0][1]["callback_data"]
    selected_action(db, callback, NOW, "owner")
    db.flush()
    assert db.get(AppState, "conversation:pending") is None


def test_delayed_delivery_activates_selectors_without_renewing_sent_reply(db, db_engine):
    now = datetime.now(UTC)
    history_page(db, now - timedelta(hours=1))
    keyboard = db.info["reply_keyboard"]
    callback = keyboard["inline_keyboard"][-1][0]["callback_data"]
    # Another page cleans old delivered selectors, but must retain an unsent reply.
    history_page(db, now)
    db.commit()

    class Bot:
        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=1)

    bot = Bot()
    asyncio.run(deliver(bot, db_engine, 1, "delayed-history", "History", keyboard=keyboard))
    key = "telegram:selection:" + callback[2:]
    value = db.get(AppState, key, populate_existing=True).value
    assert value["delivered"] and datetime.fromisoformat(value["expires_at"]) > now + timedelta(
        minutes=14
    )
    asyncio.run(deliver(bot, db_engine, 1, "delayed-history", "History", keyboard=keyboard))
    assert db.get(AppState, key, populate_existing=True).value == value
    assert "устарела" not in selected_action(db, callback, datetime.now(UTC), "owner")


def test_close_picker_back_keeps_open_episode_actions(db):
    for hours in (1, 2):
        create_event(
            db,
            EventInput(start=NOW - timedelta(hours=hours), payload={"type": "migraine"}),
            actor="owner",
        )
    history_page(db, NOW, open_only=True)
    selected_action(
        db, db.info["reply_keyboard"]["inline_keyboard"][0][0]["callback_data"], NOW, "owner"
    )
    back = db.info["reply_keyboard"]["inline_keyboard"][0][0]["callback_data"]
    page = selected_action(db, back, NOW, "owner")
    assert page.startswith("Выберите эпизод")
    assert "Завершить" in db.info["reply_keyboard"]["inline_keyboard"][0][0]["text"]


def test_callback_received_before_expiry_survives_queue_delay(db):
    event = create_event(
        db,
        EventInput(start=NOW, payload={"type": "note", "description": "synthetic"}),
        actor="owner",
    )
    history_page(db, NOW)
    callback = db.info["reply_keyboard"]["inline_keyboard"][0][0]["callback_data"]
    db.info["conversation_now"] = NOW + timedelta(hours=2)
    response = handle_button(db, callback, Settings(), "owner", 20, NOW + timedelta(minutes=14))
    assert "Выбрано" in response
    pending = db.get(AppState, "conversation:pending", populate_existing=True).value
    assert pending["event_ids"] == [str(event.id)]
    assert datetime.fromisoformat(pending["selection_expires_at"]) > db.info["conversation_now"]


def test_linked_medication_does_not_dismiss_selected_refinement(db):
    from sqlalchemy import select

    from garmin_ai.models import Event

    handle_button(db, "migraine", Settings(timezone="UTC"), "owner", 1, NOW)
    episode = db.scalar(select(Event).where(Event.kind == "migraine"))
    command = Interpretation(
        intent="log",
        confidence=1,
        events=[
            EventInput(
                start=NOW,
                timezone="UTC",
                payload={
                    "type": "medication",
                    "name": "synthetic",
                    "dose": 1,
                    "unit": "tablet",
                    "reason_event_id": episode.id,
                },
            )
        ],
    )
    result = interpret(
        db, Provider(command), "принял лекарство от этой мигрени", Settings(timezone="UTC"), NOW
    )
    assert not result._dismiss_refinement
    assert result.intent == "clarify"


def test_history_preserves_mandatory_medication_context(db):
    handle_button(db, "medication", Settings(), "owner", 1, NOW)
    previous = db.get(AppState, "conversation:pending", populate_existing=True).value
    history_page(db, NOW)
    assert db.get(AppState, "conversation:pending", populate_existing=True).value == previous


def test_abandoned_unsent_selectors_have_bounded_retention(db):
    from sqlalchemy import select

    from garmin_ai.telegram_history import PREFIX

    history_page(db, NOW)
    old_key = db.scalar(select(AppState.key).where(AppState.key.startswith(PREFIX)))
    history_page(db, NOW + timedelta(days=1))
    assert db.get(AppState, old_key) is not None
    history_page(db, NOW + timedelta(days=8))
    assert db.get(AppState, old_key, populate_existing=True) is None


def test_edit_received_before_deadline_survives_processing_delay(db):
    event = create_event(
        db,
        EventInput(start=NOW, payload={"type": "note", "description": "synthetic"}),
        actor="owner",
    )
    history_page(db, NOW)
    callback = db.info["reply_keyboard"]["inline_keyboard"][0][0]["callback_data"]
    selected_action(db, callback, NOW, "owner")
    db.info["conversation_now"] = NOW + timedelta(hours=3)
    command = Interpretation(
        intent="update",
        confidence=1,
        target_event_id=event.id,
        changed_fields=["payload.description"],
        events=[
            EventInput(start=NOW, payload={"type": "note", "description": "corrected synthetic"})
        ],
    )
    result = interpret(
        db, Provider(command), "corrected synthetic", Settings(), NOW + timedelta(minutes=14)
    )
    assert result.intent == "update", result.clarification
