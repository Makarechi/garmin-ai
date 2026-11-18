"""Record two synthetic diary clarifications through the pinned Python agent."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.agent import Interpretation, apply_command
from garmin_ai.models import AppState


def capture(output):
    cases = [
        ("2026-11-18T09:30:00+00:00", "synthetic report", "What time did it start?"),
        ("2026-11-18T09:40:00+00:00", "synthetic answer", "Was that in the morning?"),
    ]
    results = []
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            session.info["channel_destination_instance_id"] = "telegram:primary"
            for update_id, (stamp, text, question) in enumerate(cases, 301):
                sent = datetime.fromisoformat(stamp)
                processed = sent.replace(second=1, microsecond=120000, tzinfo=UTC)
                session.info["conversation_now"] = processed
                response = apply_command(
                    session,
                    Interpretation(intent="clarify", confidence=0.9, clarification=question),
                    text=text,
                    update_id=update_id,
                    actor="telegram:42",
                    now=sent,
                )
                form = session.get(AppState, "conversation:pending", populate_existing=True)
                assert form is not None
                results.append(
                    {
                        "update_id": update_id,
                        "message_at": stamp,
                        "processing_started": processed.isoformat(),
                        "text": text,
                        "outbox_text": response,
                        "form": form.value,
                    }
                )
        output.write_text(json.dumps(results), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
