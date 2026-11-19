"""Capture real local Telegram tracker entry conversations on disposable data."""

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.accounts import bind_channel
from garmin_ai.config import Settings
from garmin_ai.definitions import activate_definition, create_definition_draft
from garmin_ai.models import AppState, Audit, Event, EventDefinition, EventDefinitionVersion
from garmin_ai.share_policy import TrackerShareConsent, grant_tracker_share, revoke_tracker_share
from garmin_ai.telegram import process_message, save_update

PENDING = "conversation:pending"
VOICE = {
    "file_id": "synthetic-form-voice",
    "file_unique_id": "synthetic-form-unique",
    "duration": 1,
    "file_size": 128,
    "mime_type": "audio/ogg",
}


def seed(engine):
    definitions = [
        ("text", "point", {"type": "string", "minLength": 1, "maxLength": 50}),
        ("integer", "point", {"type": "integer", "minimum": -2, "maximum": 5}),
        ("number", "point", {"type": "number", "minimum": -2, "maximum": 5}),
        ("boolean", "point", {"type": "boolean"}),
        (
            "choice",
            "point",
            {"type": "string", "enum": ["", " ", "/skip", "=", "Straße", "A", "B"]},
        ),
        (
            "json",
            "point",
            {
                "type": "array",
                "items": {"type": "string", "maxLength": 12},
                "minItems": 1,
                "maxItems": 2,
            },
        ),
        ("open", "open_interval", {"type": "string", "maxLength": 50}),
        ("bounded", "bounded_interval", {"type": "string", "maxLength": 50}),
        ("flexible", "flexible", {"type": "string", "maxLength": 50}),
        ("constant", "point", {"type": "boolean", "const": True}),
    ]
    definitions.append(("sensitive", "point", {"type": "string", "maxLength": 50}))
    with Session(engine) as session:
        bind_channel(
            session,
            channel="telegram",
            channel_instance_id="primary",
            external_id="42",
            confirmed=True,
        )
        for name, topology, value in definitions:
            key = f"user.synthetic_chat_{name}"
            spec = {
                "key": key,
                "labels": {"en": f"Synthetic {name}", "ru": f"Синтетический {name}"},
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"value": value},
                    "required": ["value"],
                },
                "fields": {
                    "value": {
                        "id": f"{key}.value",
                        "labels": {"en": "Synthetic [value]!", "ru": "Синтетическое [поле]!"},
                        "semantic": "nominal",
                    }
                },
                "topology": topology,
                "privacy": "sensitive" if name == "sensitive" else "private",
            }
            draft = create_definition_draft(
                session, spec, actor="synthetic-fixture", authorized=True
            )
            activate_definition(
                session, draft.id, draft.revision, actor="synthetic-fixture", authorized=True
            )
        key = "user.synthetic_chat_wide"
        names = [f"value{index}" for index in range(1, 7)]
        spec = {
            "key": key,
            "labels": {"en": "Synthetic wide", "ru": "Синтетический широкий"},
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    name: {"type": "string", "minLength": 1, "maxLength": 4096} for name in names
                },
                "required": names,
            },
            "fields": {
                name: {
                    "id": f"{key}.{name}",
                    "labels": {"en": f"Synthetic {name}", "ru": f"Синтетическое {name}"},
                    "semantic": "nominal",
                }
                for name in names
            },
            "topology": "point",
        }
        draft = create_definition_draft(session, spec, actor="synthetic-fixture", authorized=True)
        activate_definition(
            session, draft.id, draft.revision, actor="synthetic-fixture", authorized=True
        )
        session.commit()


def cases():
    rows = []

    def create(name):
        rows.append({"create": name})

    def answer(text, **options):
        rows.append({"message": {"text": text}, **options})

    create("text")
    answer("bad time")
    answer("2026-11-18 10:00Z")
    answer("/skip")
    answer("=/empty")
    answer("==/empty")
    rows.append({"history": "edit", "entry": "text"})
    answer("=")
    answer("==")
    create("integer")
    answer("2026-11-18 11:00Z")
    for text in ["1.0", "--1", "²", "6", "-3", "٣"]:
        answer(text)
    create("number")
    answer("2026-11-18 12:00Z")
    for text in ["NaN", "1,5", "0.1234567890123456789", "0.5"]:
        answer(text)
    create("boolean")
    answer("2026-11-18 13:00Z")
    answer("sometimes")
    answer("ДА")
    create("choice")
    answer("2026-11-18 14:00Z")
    answer("unlisted")
    answer("STRASSE")
    create("json")
    answer("2026-11-18 15:00Z")
    for text in ['{"x":1,"x":2}', '"\\ud800"', "[1]", "[]", '["synthetic"]']:
        answer(text)
    create("open")
    answer("2026-11-18 16:00Z")
    answer("2026-11-18 15:59Z")
    answer("none")
    answer("Synthetic open entry")
    rows.append({"history": "close", "entry": "open"})
    answer("2026-11-18 16:00Z")
    answer("invalid")
    answer("2026-11-18 16:30Z")
    create("bounded")
    answer("2026-11-18 17:00Z")
    for text in ["none", "2026-11-18 17:00Z", "2026-11-18 18:00Z"]:
        answer(text)
    answer("Synthetic bounded entry")
    create("flexible")
    answer("2026-11-18 19:00Z")
    answer("none")
    answer("Synthetic flexible point")
    rows.append({"history": "edit", "entry": "flexible"})
    answer("=")
    answer("=")
    answer("=")
    create("constant")
    answer("2026-11-18 20:00Z")
    create("text")
    answer("2026-11-18 21:00Z", patch={"prompt_order": [0, 999999]})
    answer("2026-11-18 21:00Z", patch={"prompt_order": [0, 0]})
    answer("/cancel")
    create("text")
    rows.append({"message": {"voice": VOICE, "caption": "2026-11-18 22:00Z"}})
    rows.append({"message": {"voice": VOICE}})
    answer("Synthetic after voice")
    create("sensitive")
    rows.append({"create": "sensitive", "grant": ["schema"]})
    answer("2026-11-18 23:00Z")
    answer("Synthetic sensitive entry")
    rows.append({"history": "edit", "entry": "sensitive"})
    rows.append({"history": "edit", "entry": "sensitive", "grant": ["schema", "facts"]})
    answer("=")
    answer("Synthetic denied update", revoke=True)
    rows.append({"create": "sensitive", "grant": ["schema"]})
    answer("2026-11-18 23:30Z", revoke=True)
    create("wide")
    answer("2026-11-18 23:40Z")
    for _ in range(6):
        answer("🧪" * 1100)
    for index in range(6):
        answer(f"Synthetic shortened {index}")
    create("text")
    answer("2026-11-18 23:50Z")
    answer("Synthetic recovered entry", recover=True)
    return rows


def versions(session):
    return {
        str(row.id): session.get(EventDefinition, row.definition_id).key
        for row in session.scalars(select(EventDefinitionVersion)).all()
    }


def normalized(value, versions_map):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"created_at", "prompt_advanced_at"}:
                assert datetime.fromisoformat(item).utcoffset() is not None
                continue
            if key == "submission_id" and item is not None:
                assert re.fullmatch(r"[0-9a-f]{32}", item)
                item = "synthetic-submission"
            result[key] = normalized(item, versions_map)
        return result
    if isinstance(value, list):
        return [normalized(item, versions_map) for item in value]
    if isinstance(value, str):
        for identity, key in versions_map.items():
            value = value.replace(identity, key)
    return value


def snapshot(session):
    mapping = versions(session)
    for event in session.scalars(
        select(Event).where(Event.kind.startswith("user.synthetic_chat_"))
    ):
        mapping[str(event.id)] = f"entry:{event.kind}:{event.start.isoformat()}"
    pending = session.get(AppState, PENDING, populate_existing=True)
    events = []
    for event in session.scalars(
        select(Event)
        .where(Event.kind.startswith("user.synthetic_chat_"))
        .order_by(Event.start, Event.kind, Event.id)
    ):
        events.append(
            {
                "kind": event.kind,
                "start": event.start.isoformat(),
                "end": event.end.isoformat() if event.end else None,
                "timezone": event.timezone,
                "topology": event.topology,
                "revision": event.revision,
                "deleted": event.deleted,
                "source": event.source,
                "payload": event.payload,
            }
        )
    audits = Counter()
    for audit in session.scalars(
        select(Audit)
        .join(Event, Event.id == Audit.event_id)
        .where(Event.kind.startswith("user.synthetic_chat_"))
    ):
        event = session.get(Event, audit.event_id)
        audits[(event.kind, event.start.isoformat(), audit.action, audit.actor)] += 1
    return {
        "pending": normalized(pending.value, mapping) if pending else None,
        "events": events,
        "audit": [[*key, count] for key, count in sorted(audits.items())],
    }


def capture(engine, locale="en", *, seed_data=seed, conversation_cases=cases, provider=None,
            capture_state=snapshot):
    seed_data(engine)
    outputs = []
    for index, case in enumerate(conversation_cases()):
        update_id = 71001 + index
        with Session(engine) as session:
            for key, value in case.get("states", {}).items():
                old = session.get(AppState, key)
                if old:
                    session.delete(old)
                    session.flush()
                if value is not None:
                    session.add(AppState(key=key, value=value))
            for name, status in case.get("tracker_status", {}).items():
                definition = session.scalar(
                    select(EventDefinition).where(
                        EventDefinition.key == f"user.synthetic_chat_{name}"
                    )
                )
                definition.status = status
            if "legacy_tracker" in case:
                version = session.scalar(
                    select(EventDefinitionVersion)
                    .join(EventDefinition)
                    .where(
                        EventDefinition.key == f"user.synthetic_chat_{case['legacy_tracker']}"
                    )
                )
                pending = session.get(AppState, PENDING)
                if pending:
                    session.delete(pending)
                    session.flush()
                session.add(
                    AppState(
                        key=PENDING,
                        value={
                            "definition_version_id": str(version.id),
                            "button": "tracker_form",
                            "action": "log",
                            "event_ids": [],
                            "question": "Synthetic legacy tracker prompt",
                            "channel_instance_id": "telegram:primary",
                            "created_at": datetime.now(UTC).isoformat(),
                        },
                    )
                )
            if "grant" in case or case.get("revoke"):
                definition = session.scalar(
                    select(EventDefinition).where(
                        EventDefinition.key == "user.synthetic_chat_sensitive"
                    )
                )
                if "grant" in case:
                    grant_tracker_share(
                        session,
                        TrackerShareConsent(
                            definition_id=definition.id,
                            destination_kind="channel",
                            destination_instance_id="telegram:primary",
                            categories=set(case["grant"]),
                            granted_at=datetime.now(UTC),
                        ),
                        authorized=True,
                    )
                else:
                    assert revoke_tracker_share(
                        session, definition.id, "channel", "telegram:primary", authorized=True
                    )
            if "patch" in case:
                pending = session.get(AppState, PENDING)
                assert pending
                pending.value = {**pending.value, **case["patch"]}
            if "create" in case:
                version = session.scalar(
                    select(EventDefinitionVersion)
                    .join(EventDefinition)
                    .where(EventDefinition.key == f"user.synthetic_chat_{case['create']}")
                )
                callback = f"create:{version.id}"
            elif "history" in case:
                event = session.scalar(
                    select(Event)
                    .where(Event.kind == f"user.synthetic_chat_{case['entry']}")
                    .order_by(Event.start.desc())
                )
                token = f"synthetic_form_{index}"
                session.add(
                    AppState(
                        key=f"telegram:selection:{token}",
                        value={
                            "action": case["history"],
                            "event_id": str(event.id),
                            "revision": event.revision,
                            "channel_instance_id": "telegram:primary",
                            "expires_at": "2100-01-01T00:00:00+00:00",
                        },
                    )
                )
                callback = f"h:{token}"
            else:
                callback = None
            now = int(datetime.now(UTC).timestamp())
            if callback:
                wire = {
                    "update_id": update_id,
                    "callback_query": {
                        "id": f"synthetic-form-{index}",
                        "data": callback,
                        "from": {"id": 42},
                        "message": {
                            "message_id": update_id,
                            "date": now,
                            "chat": {"id": 42, "type": "private"},
                        },
                    },
                }
            else:
                wire = {
                    "update_id": update_id,
                    "message": {
                        "message_id": update_id,
                        "date": now,
                        "from": {"id": 42},
                        "chat": {"id": 42, "type": "private"},
                        **case["message"],
                    },
                }
            assert save_update(session, wire, 42, callback_time_known=True)
            session.commit()
        reply = process_message(
            engine,
            provider,
            Settings(_env_file=None, telegram_user_id=42, locale=locale, timezone="UTC"),
            update_id,
            transcript="" if case.get("message", {}).get("voice") else None,
        )
        with Session(engine) as session:
            outputs.append({**case, "reply": reply, "snapshot": capture_state(session),
                            **({"source_message_date": now} if "legacy_tracker" in case else {})})
    return outputs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        args.output.write_text(json.dumps(capture(engine), ensure_ascii=False, indent=2) + "\n")
    finally:
        engine.dispose()
