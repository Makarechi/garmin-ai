"""Capture durable local tracker setup replies and state from frozen Python."""

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.accounts import bind_channel
from garmin_ai.config import Settings
from garmin_ai.models import AppState, TelegramUpdate
from garmin_ai.telegram import TELEGRAM_INSTANCE, _storage_update_id, process_message, save_update

KEY = "tracker:chat-setup:telegram:primary"
CLOCK_FIELDS = ["started_at", "last_activity_at", "last_prompt_advanced_at", "preview_issued_at"]
VOICE = {
    "file_id": "synthetic-setup-voice",
    "file_unique_id": "synthetic-setup-unique",
    "duration": 1,
    "mime_type": "audio/ogg",
    "file_size": 128,
}


def cases():
    values = []

    def add(answer, **options):
        values.append({"message": {"text": answer}, **options})

    add("/newtracker", pending=True)
    add("/cancel")
    add("/newtracker extra")
    add("/newtracker")
    add("/preview", patch={"key": "chat_synthetic_first"})
    add("/privacy sensitive")
    add("\u001c \t")
    add("I can't breathe")
    add("A" * 65)
    add("Synthetic [focus]_tracker!")
    values.append({"callback": "h:synthetic_setup_missing"})
    add("   ")
    for answer in [
        "Bad field",
        "Count | count -1-3",
        "Count | count 0-9007199254740993",
        "Rating | scale 0-21",
        "Choice | choice A,A",
    ]:
        add(answer)
    add("Rating | scale -2-3")
    add("Count | count ０-９")
    add("Note | text")
    add("Sudden severe chest pain | text")
    add("Choice | choice A, B")
    add("/remove_field")
    add("/preview")
    add("/cancel extra")
    add("/privacy invalid")
    add("/privacy private")
    add("/confirm_tracker")
    add("/privacy sensitive")
    add("/preview")
    add("/confirm_tracker", expire_preview=True)
    add("/preview")
    add("/confirm_tracker")
    add("/newtracker")
    add("Synthetic name", patch={"key": "chat_synthetic_second", "locale": "ru"})
    values.append({"message": {"voice": VOICE}})
    add("Field | boolean")
    add("/preview")
    add("Legacy stale | text", patch={"last_provider_update_id": None, "last_update_id": 999999})
    add("/preview", patch={"last_provider_update_id": 0, "last_update_id": 0})
    add("Epoch stale | text", patch={"started_ordering_epoch": 1, "started_provider_update_id": 0})
    add("/preview", patch={"started_ordering_epoch": 0})
    add("Stale field | text", patch={"last_provider_update_id": 999999})
    add(
        "/confirm_tracker",
        patch={
            "last_provider_update_id": 0,
            "last_prompt_advanced_at": "2100-01-01T00:00:00+00:00",
        },
    )
    add("/preview", patch={"last_prompt_advanced_at": "2000-01-01T00:00:00+00:00"})
    add("Too early | text", patch={"started_at": "2100-01-01T00:00:00+00:00"})
    add("/cancel", patch={"started_at": "2000-01-01T00:00:00+00:00"})
    values.append({"message": {"voice": VOICE, "caption": "/newtracker"}})
    add("Synthetic voice draft", patch={"key": "chat_synthetic_voice", "locale": "zz"})
    values.append({"message": {"voice": VOICE}})
    values.append({"message": {"voice": VOICE, "caption": "/cancel"}})
    add("/newtracker")
    add(
        "Synthetic delayed name",
        sent_delta=-2 * 86400,
        patch={
            "key": "chat_synthetic_delayed",
            "started_at": "2000-01-01T00:00:00+00:00",
            "last_activity_at": "2000-01-01T00:00:00+00:00",
        },
        activity_delta=-2 * 86400,
    )
    add("/cancel")
    add("/newtracker")
    add(
        "/newtracker",
        patch={
            "started_at": "2000-01-01T00:00:00+00:00",
            "last_activity_at": "2000-01-01T00:00:00+00:00",
        },
    )
    add("Synthetic many fields", patch={"key": "chat_synthetic_many"})
    for index in range(33):
        add(f"Synthetic {index} | boolean")
    add("/preview")
    add("/cancel")
    add("/newtracker")
    add("Synthetic large schema", patch={"key": "chat_synthetic_large"})
    for index in range(8):
        choices = ",".join(f"{choice:02}" + "x" * 118 for choice in range(50))
        add(f"Synthetic choice {index} | choice {choices}")
    add("/preview")
    add("/remove_field")
    add("/preview")
    add("/cancel")
    return values


def normalize(draft):
    if draft is None:
        return None
    draft = dict(draft)
    for field in CLOCK_FIELDS:
        stamp = draft.pop(field, None)
        if stamp is not None:
            assert datetime.fromisoformat(stamp).utcoffset() is not None
    if re.fullmatch(r"chat_[0-9a-f]{16}", draft["key"]):
        draft["key"] = "<generated>"
    if draft.get("confirmation_token") is not None:
        assert re.fullmatch(r"[0-9a-f]{64}", draft["confirmation_token"])
        draft["confirmation_token"] = "<token>"
    return draft


def snapshot(engine):
    with Session(engine) as session:
        draft = session.get(AppState, KEY)
        definitions = session.execute(
            text("""SELECT key,status,revision,current_version
            FROM event_definitions WHERE namespace='user' ORDER BY key""")
        ).all()
        trackers = session.execute(
            text("""SELECT d.key,t.shortcut,t.reminder_enabled,
            t.reminder_time,t.reminder_timezone,t.revision,t.settings
            FROM tracker_configs t JOIN event_definitions d ON d.id=t.definition_id ORDER BY d.key""")
        ).all()
        return {
            "draft": normalize(draft.value if draft else None),
            "definitions": [list(row) for row in definitions],
            "trackers": [list(row) for row in trackers],
        }


def before(session, case):
    if case.get("pending"):
        session.add(
            AppState(
                key="conversation:pending",
                value={
                    "created_at": datetime.now(UTC).isoformat(),
                    "channel_instance_id": "telegram:primary",
                    "button": "note",
                },
            )
        )
    if "patch" in case:
        row = session.get(AppState, KEY)
        assert row is not None
        row.value = {**row.value, **case["patch"]}
    if "activity_delta" in case:
        row = session.get(AppState, KEY)
        row.value = {
            **row.value,
            "last_activity_at": (
                datetime.now(UTC) + timedelta(seconds=case["activity_delta"])
            ).isoformat(),
        }
    if case.get("expire_preview"):
        for row in session.scalars(select(AppState).where(AppState.key.like("tracker-preview:%"))):
            row.value = {**row.value, "expires_at": "2000-01-01T00:00:00+00:00"}


def seed(engine):
    with Session(engine) as session:
        bind_channel(
            session,
            channel="telegram",
            channel_instance_id="primary",
            external_id="42",
            confirmed=True,
        )
        session.add(
            TelegramUpdate(
                id=50103,
                status="processed",
                payload={
                    "update_id": 50103,
                    "_channel_instance": {"channel": "telegram", "instance_id": "other"},
                    "message": {
                        "message_id": 50103,
                        "date": int(datetime.now(UTC).timestamp()),
                        "from": {"id": 42},
                        "chat": {"id": 42, "type": "private"},
                        "text": "Synthetic other bot history",
                    },
                },
            )
        )
        session.commit()


def capture(engine):
    seed(engine)
    result = []
    for index, case in enumerate(cases(), start=50101):
        with Session(engine) as session:
            before(session, case)
            now = datetime.now(UTC)
            message = {
                "message_id": index,
                "date": int((now + timedelta(seconds=case.get("sent_delta", 0))).timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                **case.get("message", {}),
            }
            wire = {"update_id": index, "message": message}
            if "callback" in case:
                wire = {
                    "update_id": index,
                    "callback_query": {
                        "id": f"synthetic-setup-callback-{index}",
                        "from": {"id": 42},
                        "message": message,
                        "data": case["callback"],
                    },
                }
            assert save_update(session, wire, 42)
            storage = _storage_update_id(session, index, TELEGRAM_INSTANCE)
            session.commit()
        caption_command = case.get("message", {}).get("voice") and case["message"].get(
            "caption", ""
        ).strip().startswith("/")
        reply = process_message(
            engine,
            None,
            Settings(_env_file=None, telegram_user_id=42, locale="en", timezone="UTC"),
            storage,
            transcript="" if caption_command else None,
        )
        result.append({**case, "update": index, "reply": reply, "snapshot": snapshot(engine)})
    assert len(result[-1]["snapshot"]["trackers"]) == 1
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        result = capture(engine)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print("Captured tracker setup steps", len(result), flush=True)
    finally:
        engine.dispose()
