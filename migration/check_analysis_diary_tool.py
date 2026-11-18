"""Exercise the authenticated Rust diary tool against the pinned model read."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from check_model_diary_policy import END, START, seed
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.models import ChannelBinding, Conversation, InboundMessage, Person
from garmin_ai.tools import call_tool, model_tool_descriptions


def record_questions(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id))
        session.execute(
            text("""
            UPDATE app_state SET value=value || '{
              "channel":{"channel":"telegram","instance_id":"telegram:allowed"},
              "fallback_channels":[{"channel":"telegram","instance_id":"telegram:schema"}]
            }'::jsonb WHERE key='preferences:onboarding'
            """)
        )
        for index, suffix in enumerate(("allowed", "schema")):
            channel = f"telegram:{suffix}"
            chat = str(900001 + index)
            conversation = UUID(int=100 + index)
            session.add(
                ChannelBinding(
                    owner_id=owner,
                    channel="telegram",
                    channel_instance_id=channel,
                    external_id=chat,
                    confirmation_method="synthetic",
                )
            )
            session.add(
                Conversation(
                    id=conversation,
                    owner_id=owner,
                    channel="telegram",
                    channel_instance_id=channel,
                    external_conversation_id=chat,
                    share_owner_memory=True,
                )
            )
            session.flush()
            session.add(
                InboundMessage(
                    id=UUID(int=200 + index),
                    operation_id=UUID(int=300 + index),
                    conversation_id=conversation,
                    owner_id=owner,
                    channel="telegram",
                    channel_instance_id=channel,
                    external_event_id=f"synthetic:{index}",
                    sender_ref=chat,
                    received_at=START,
                    kind="text",
                    normalized_text="Synthetic diary question",
                    envelope={},
                )
            )


def verify(binary):
    engine = isolated_engine()
    try:
        seed(engine)
        record_questions(engine)
        args = {"start": START.isoformat(), "end": END.isoformat()}
        cases = []
        for index, suffix, model in (
            (0, "allowed", "model:primary"),
            (1, "schema", "model:primary"),
            (0, "allowed", "model:other"),
        ):
            with Session(
                engine,
                info={
                    "model_provider_instance_id": model,
                    "channel_destination_instance_id": f"telegram:{suffix}",
                },
            ) as session:
                menu = [
                    item for item in model_tool_descriptions(session) if item["name"] == "events"
                ]
                assert len(menu) == 1
                expected = call_tool(session, "events", args, for_model=True)
                cases.append({"index": index, "model": model, "result": expected})
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "analysis-diary-tool.json"
            fixture.write_text(
                json.dumps({"cases": cases, "args": args, "menu": menu[0]}), encoding="utf-8"
            )
            subprocess.run(
                [binary, "--ignored", "--nocapture"],
                env={**os.environ, "GA_TEST_ANALYSIS_DIARY": str(fixture)},
                check=True,
                timeout=30,
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    verify(parser.parse_args().binary.resolve(strict=True))
