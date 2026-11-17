"""Capture outbox generation fences and deduplication using pinned Python."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.channels import OutboundIntent
from garmin_ai.dialogue import DialogueService
from garmin_ai.models import Conversation, InboundMessage, Person

NOW = datetime.fromisoformat("2026-11-17T12:00:00+00:00")
TARGET, SOURCE, SOURCE2, TARGET_EPOCH, SOURCE_EPOCH, SOURCE_EPOCH2, OTHER = (
    UUID(int=value) for value in (2201, 2202, 2203, 2211, 2212, 2213, 2299)
)
OPERATION, FIRST, EDIT = (UUID(int=value) for value in (2250, 2251, 2252))
CHANNEL = {"channel": "web", "instance_id": "local"}


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            owner = session.scalar(select(Person.id))
            for row_id, epoch, kind, instance in (
                (TARGET, TARGET_EPOCH, "web", "local"),
                (SOURCE, SOURCE_EPOCH, "telegram", "primary"),
                (SOURCE2, SOURCE_EPOCH2, "telegram", "fallback"),
            ):
                session.add(
                    Conversation(
                        id=row_id,
                        owner_id=owner,
                        channel=kind,
                        channel_instance_id=instance,
                        memory_epoch=epoch,
                        share_owner_memory=True,
                        state={},
                    )
                )
            session.flush()

            def inbound(inbound_id, revision):
                session.add(
                    InboundMessage(
                        id=inbound_id,
                        owner_id=owner,
                        conversation_id=TARGET,
                        channel="web",
                        channel_instance_id="local",
                        external_event_id=f"synthetic-{revision}",
                        sender_ref="fixture",
                        received_at=NOW,
                        kind="text" if revision == 1 else "edit",
                        normalized_text="synthetic question",
                        envelope={},
                        revision=revision,
                        operation_id=OPERATION,
                    )
                )
                session.flush()

            inbound(FIRST, 1)
            results = []
            service = DialogueService()
            sources = {SOURCE2: SOURCE_EPOCH2, SOURCE: SOURCE_EPOCH}

            def attempt(name, number, *, epoch=TARGET_EPOCH, dependencies=None, item=FIRST):
                dependencies = sources if dependencies is None else dependencies
                intent = {
                    "intent_id": str(UUID(int=number)),
                    "owner_id": str(owner),
                    "conversation_id": str(TARGET),
                    "channel_instance": CHANNEL,
                    "blocks": [{"text": f"synthetic generated reply {number}"}],
                }
                receipt = service.queue_generation_result(
                    session,
                    OutboundIntent.model_validate(intent),
                    expected_epoch=epoch,
                    source_epochs=dependencies,
                    operation_id=OPERATION,
                    inbound_message_id=item,
                )
                results.append(
                    {
                        "name": name,
                        "intent": intent,
                        "inbound": str(item),
                        "request_fence": {
                            "target_epoch": str(epoch),
                            "source_epochs": {
                                str(key): str(value) for key, value in dependencies.items()
                            },
                        },
                        "row": (
                            {
                                "id": str(receipt.id),
                                "key": receipt.dedup_key,
                                "fence": receipt.memory_fence,
                            }
                            if receipt
                            else None
                        ),
                    }
                )

            attempt("first", 2271)
            attempt("same_revision", 2272)
            attempt("target_forgotten", 2273, epoch=OTHER)
            attempt("source_missing", 2274, dependencies={SOURCE: SOURCE_EPOCH})
            attempt("source_forgotten", 2275, dependencies={**sources, SOURCE: OTHER})
            inbound(EDIT, 2)
            attempt("old_revision", 2276)
            attempt("current_revision", 2277, item=EDIT)
            output.write_text(
                json.dumps(
                    {
                        "now": NOW.isoformat(),
                        "target": str(TARGET),
                        "operation": str(OPERATION),
                        "results": results,
                    }
                ),
                encoding="utf-8",
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
