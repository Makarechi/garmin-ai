"""Capture permitted local tracker selection and subsequent entry forms."""

import capture_tracker_chat_entries as entries
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.models import EventDefinition, TrackerConfig

original_seed = entries.seed
original_cases = entries.cases


def seed(engine):
    original_seed(engine)
    with Session(engine) as session:
        for name in ["text", "sensitive"]:
            definition = session.scalar(
                select(EventDefinition).where(EventDefinition.key == f"user.synthetic_chat_{name}")
            )
            session.add(
                TrackerConfig(
                    owner_id=definition.owner_id,
                    definition_id=definition.id,
                    shortcut="Focus",
                )
            )
        session.commit()


def cases(locale):
    rows = []

    def answer(text, **options):
        rows.append({"message": {"text": text}, **options})

    for request in ["log Focus", "I logged tracker Focus", "запиши трекер Focus"]:
        answer(request)
        answer("2026-11-18 01:00Z")
        answer("Synthetic locally selected")
    label = "Синтетический" if locale == "ru" else "Synthetic"
    answer(f"log {label}")
    answer("/unrecognized")
    for choice in ["２", "0", "6", "999999999999999999999999999999999999", "arbitrary"]:
        answer(choice)
    answer("2")
    answer("/cancel")
    answer(f"log {label}")
    answer("1", patch={"prompt_order": [0, 999999]})
    answer("1", patch={"prompt_order": [0, 0]})
    answer("/cancel")
    answer("log Focus", grant=["schema"])
    answer("not a choice")
    answer("1", revoke=True)
    answer("log Focus")
    answer("/cancel")
    answer("log my Focus", grant=["schema"])
    answer("２")
    rows.append({"message": {"voice": entries.VOICE}})
    answer("2")
    answer("2026-11-18 02:00Z")
    answer("Synthetic selected second")
    rows.append({"message": {"voice": entries.VOICE, "caption": "log Focus"}})
    answer("1")
    answer("2026-11-18 03:00Z")
    answer("Synthetic caption selected")
    answer("log Focus")
    answer("not a choice", revoke=True)
    answer("log Focus", grant=["schema"])
    answer("1", tracker_status={"sensitive": "retired"})
    answer("log Focus", tracker_status={"sensitive": "active"})
    answer("not a choice", tracker_status={"sensitive": "retired"})
    answer("log Focus", tracker_status={"sensitive": "active"})
    answer("log Focus", patch={"channel_instance_id": "telegram:other"})
    answer("/cancel")
    analytic_key = "outbox:update:synthetic_analysis:0"
    answer(
        "log Focus",
        states={
            analytic_key: {
                "status": "sent",
                "kind": "analysis",
                "message_id": 810001,
                "channel_instance_id": "telegram:primary",
            }
        },
    )
    answer("/cancel")
    rows.append({"message": {"text": "log Focus", "reply_to_message": {"message_id": 810001}}})
    answer("log Focus", states={analytic_key: None})
    answer("/cancel")
    for message_id, analysis in [(810002, True), (810003, False)]:
        key = f"outbox:update:synthetic_native_{message_id}:0"
        reply = {
            "status": "sent",
            "message_id": message_id,
            "channel_instance_id": "telegram:primary",
        }
        if analysis:
            reply["kind"] = "analysis"
        rows.append(
            {
                "message": {"text": "log Focus", "reply_to_message": {"message_id": message_id}},
                "states": {key: reply},
                "native_reply": {"message_id": message_id, "analysis": analysis, "legacy_key": key},
            }
        )
        answer("/cancel", states={key: None})
    forms = original_cases()
    forms[-1].pop("recover")
    return rows + forms + [{"message": {"text": "log my Focus"}, "recover": True}]


def capture(engine, locale="en"):
    return entries.capture(engine, locale, seed_data=seed, conversation_cases=lambda: cases(locale))
