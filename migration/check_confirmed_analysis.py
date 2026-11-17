"""Record the pinned Python analysis memory behavior after confirmed delivery."""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.channels import DeliveryReceipt, DeliveryState, OutboundIntent
from garmin_ai.dialogue import DialogueService, queue_intent, record_delivery_receipt
from garmin_ai.models import Conversation, InboundMessage, Person

NOW = datetime.fromisoformat("2026-11-17T12:00:00.123456+00:00")
TARGET, SOURCE, TARGET_EPOCH, SOURCE_EPOCH, WRONG_EPOCH = (
    UUID(int=key) for key in (1701, 1702, 1711, 1712, 1713)
)
CHANNEL = {"channel": "web", "instance_id": "local"}


class FixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            owner = session.scalar(select(Person.id))
            target = Conversation(
                id=TARGET,
                owner_id=owner,
                channel="web",
                channel_instance_id="local",
                memory_epoch=TARGET_EPOCH,
                share_owner_memory=True,
                state={"pending": {"fixture": True}},
            )
            source = Conversation(
                id=SOURCE,
                owner_id=owner,
                channel="telegram",
                channel_instance_id="primary",
                memory_epoch=SOURCE_EPOCH,
                share_owner_memory=True,
                state={},
            )
            session.add_all([target, source])
            session.flush()
            service = DialogueService()
            events, messages = [], []
            sources = {str(SOURCE): str(SOURCE_EPOCH)}
            fence = {"target_epoch": str(TARGET_EPOCH), "source_epochs": sources}

            def seed(index, *, age=timedelta(), wide=False, edited=False, pinned=fence):
                operation_id, inbound_id, outbox_id = (
                    UUID(int=index * 10 + base) for base in (1800, 1801, 1802)
                )
                at = NOW - age
                question = "🧪" * 1001 if wide else f"synthetic question {index}"
                blocks = [
                    {"text": "я" * 1501 if wide else f"synthetic answer {index}"},
                    {"text": "next part"},
                ]
                inbox = dict(
                    owner_id=owner,
                    conversation_id=TARGET,
                    channel="web",
                    channel_instance_id="local",
                    external_event_id=str(index),
                    sender_ref="synthetic-sender",
                    received_at=at,
                    kind="text",
                    normalized_text=question,
                    envelope={},
                    operation_id=operation_id,
                )
                session.add(InboundMessage(id=inbound_id, revision=1, **inbox))
                if edited:
                    session.add(
                        InboundMessage(id=UUID(int=inbound_id.int + 2), revision=2, **inbox)
                    )
                session.flush()
                document = {
                    "intent_id": str(outbox_id),
                    "owner_id": str(owner),
                    "conversation_id": str(TARGET),
                    "channel_instance": CHANNEL,
                    "blocks": blocks,
                }
                row = queue_intent(
                    session,
                    OutboundIntent.model_validate(document),
                    operation_id=operation_id,
                    inbound_message_id=inbound_id,
                )
                row.memory_fence = pinned
                session.flush()
                messages.append(
                    {
                        "outbox_id": str(outbox_id),
                        "operation_id": str(operation_id),
                        "inbound_id": str(inbound_id),
                        "edited": edited,
                        "received_at": at.isoformat(),
                        "question": question,
                        "answer": "\n".join(block["text"] for block in blocks),
                        "intent": document,
                        "fence": pinned,
                    }
                )
                return len(messages) - 1

            def capture_case(name, index, *, state=None, expected=fence, answer=None):
                item = messages[index]
                outbox_id = UUID(item["outbox_id"])
                at = NOW + timedelta(microseconds=len(events))
                if state is not None:
                    record_delivery_receipt(
                        session,
                        outbox_id,
                        DeliveryReceipt(intent_id=outbox_id, observed_at=at, state=state),
                    )
                answer = answer if answer is not None else item["answer"]
                try:
                    result = service.remember_analysis(
                        session,
                        TARGET,
                        operation_id=UUID(item["operation_id"]),
                        outbox_id=outbox_id,
                        expected_epoch=UUID(expected["target_epoch"]),
                        source_epochs={
                            UUID(key): UUID(value)
                            for key, value in expected["source_epochs"].items()
                        },
                        question=item["question"],
                        answer=answer,
                    )
                except LookupError as error:
                    result = type(error).__name__
                except ValueError as error:
                    result = type(error).__name__
                except RuntimeError as error:
                    result = type(error).__name__
                events.append(
                    {
                        "name": name,
                        "index": index,
                        "state": state,
                        "observed_at": at.isoformat(),
                        "answer": answer,
                        "fence": expected,
                        "result": result,
                        "stored": target.state,
                    }
                )

            with patch("garmin_ai.dialogue.datetime", FixtureClock):
                base = seed(1)
                capture_case("accepted", base, state=DeliveryState.PROVIDER_ACCEPTED)
                capture_case("wrong_text", base, state=DeliveryState.DELIVERED, answer="unrelated")
                capture_case("confirmed", base)
                capture_case("duplicate", base)
                capture_case("edited", seed(2, edited=True), state=DeliveryState.READ)
                capture_case("wide", seed(3, wide=True), state=DeliveryState.READ)
                capture_case(
                    "historical",
                    seed(4, age=timedelta(days=7, microseconds=1)),
                    state=DeliveryState.DELIVERED,
                )
                capture_case(
                    "exact_week", seed(5, age=timedelta(days=7)), state=DeliveryState.DELIVERED
                )
                capture_case(
                    "future", seed(6, age=timedelta(microseconds=-1)), state=DeliveryState.DELIVERED
                )
                capture_case(
                    "stale_target",
                    seed(7),
                    state=DeliveryState.DELIVERED,
                    expected={**fence, "target_epoch": str(WRONG_EPOCH)},
                )
                capture_case(
                    "unpinned_source",
                    seed(8),
                    state=DeliveryState.DELIVERED,
                    expected={**fence, "source_epochs": {}},
                )
                capture_case(
                    "obsolete_delivered_fence",
                    seed(9, pinned={**fence, "source_epochs": {str(SOURCE): str(WRONG_EPOCH)}}),
                    state=DeliveryState.DELIVERED,
                )
                capture_case(
                    "python_json_null_fence", seed(10, pinned=None), state=DeliveryState.DELIVERED
                )
            output.write_text(
                json.dumps(
                    {
                        "now": NOW.isoformat(),
                        "target": str(TARGET),
                        "source": str(SOURCE),
                        "fence": fence,
                        "messages": messages,
                        "events": events,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
