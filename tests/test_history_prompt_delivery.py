import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from garmin_ai.agent import Interpretation, interpret
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState
from garmin_ai.telegram import deliver
from garmin_ai.telegram_history import history_page, selected_action


def test_delayed_edit_prompt_renews_its_selection_but_sent_retry_does_not(db, db_engine):
    now = datetime.now(UTC)
    earlier = now - timedelta(hours=3)
    row = create_event(
        db,
        EventInput(start=earlier, payload={"type": "note", "description": "synthetic"}),
        actor="owner",
    )
    history_page(db, earlier)
    callback = db.info["reply_keyboard"]["inline_keyboard"][0][0]["callback_data"]
    selected_action(db, callback, earlier, "owner")
    keyboard = db.info["reply_keyboard"]
    db.commit()

    class Bot:
        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=1)

    asyncio.run(deliver(Bot(), db_engine, 1, "late-edit-prompt", "synthetic", keyboard))
    pending = db.get(AppState, "conversation:pending", populate_existing=True).value
    assert datetime.fromisoformat(pending["selection_expires_at"]) > now + timedelta(minutes=14)
    assert datetime.fromisoformat(pending["selected_at"]) == earlier
    asyncio.run(deliver(Bot(), db_engine, 1, "late-edit-prompt", "synthetic", keyboard))
    assert db.get(AppState, "conversation:pending", populate_existing=True).value == pending

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="update",
                target_event_id=row.id,
                confidence=1,
                changed_fields=["payload.description"],
                events=[
                    EventInput(start=earlier, payload={"type": "note", "description": "edited"})
                ],
            )

    result = interpret(db, Provider(), "исправь описание", Settings(), datetime.now(UTC))
    assert result.intent == "update"


def test_old_reply_does_not_renew_an_unrelated_pending_prompt(db):
    from garmin_ai.telegram_history import renew_selectors

    now = datetime.now(UTC)
    history_page(db, now)
    keyboard = db.info["reply_keyboard"]
    original = {"selection_prompt": "another", "created_at": now.isoformat()}
    db.add(AppState(key="conversation:pending", value=original))
    db.flush()
    renew_selectors(db, keyboard, now + timedelta(hours=3), delivered=True)
    assert db.get(AppState, "conversation:pending").value == original
