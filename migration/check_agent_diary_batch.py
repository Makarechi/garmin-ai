"""Capture linked diary writes and rollback from the pinned Python batch handler."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from garmin_ai.agent import Interpretation
from garmin_ai.event_batches import create_batch
from garmin_ai.models import Audit, Event, MetricObservation, TimelineInterval

AT = "2026-11-17T12:00:00+00:00"
TEXT = " synthetic user evidence 🧪 "
PARENT = {"start": AT, "payload": {"type": "migraine", "severity": 6}, "original_text": "invented"}
CHILD = {
    "start": AT,
    "payload": {"type": "medication", "name": "synthetic", "dose": 1, "unit": "tablet"},
    "original_text": "invented",
}


def capture(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            observations = []

            def snapshot():
                rows = session.scalars(select(Event).order_by(Event.idempotency_key)).all()
                keys = {str(row.id): row.idempotency_key for row in rows}
                return {
                    "events": [
                        {
                            "key": row.idempotency_key,
                            "kind": row.kind,
                            "source": row.source,
                            "status": row.status,
                            "text": row.original_text,
                            "parent_key": keys.get(row.payload.get("reason_event_id")),
                        }
                        for row in rows
                    ],
                    "audits": [
                        {
                            "key": keys[str(row.event_id)],
                            "action": row.action,
                            "operation": str(row.operation_id),
                            "actor": row.actor,
                        }
                        for row in session.scalars(select(Audit).order_by(Audit.id))
                    ],
                    "metrics": session.scalar(select(func.count()).select_from(MetricObservation)),
                    "intervals": session.scalar(select(func.count()).select_from(TimelineInterval)),
                }

            def attempt(name, prefix, mode, command):
                operation = UUID(int=mode)
                model = Interpretation.model_validate(command)
                reports = [
                    event.model_copy(
                        update={
                            "original_text": TEXT,
                            "source": "telegram_text",
                            "status": "confirmed",
                        },
                    )
                    for event in model.events
                ]
                try:
                    entries = create_batch(
                        session,
                        reports,
                        model.draft_links,
                        actor="owner",
                        update_id=prefix,
                        idempotency_prefix=prefix,
                        operation_id=operation,
                    )
                    order = [entry.idempotency_key for entry in entries]
                except ValueError:
                    order = None
                observations.append(
                    {
                        "name": name,
                        "prefix": prefix,
                        "operation": str(operation),
                        "command": command,
                        "order": order,
                        "state": snapshot(),
                    }
                )

            batch = {
                "intent": "log",
                "confidence": 0.9,
                "events": [CHILD, PARENT],
                "draft_links": [{"child_index": 0, "parent_index": 1}],
            }
            attempt("linked", "telegram:4401", 4101, batch)
            attempt("repeat", "telegram:4401", 4101, batch)
            bad = {**CHILD, "payload": {**CHILD["payload"], "reason_event_id": str(UUID(int=4999))}}
            attempt(
                "rollback",
                "telegram:4402",
                4102,
                {"intent": "log", "confidence": 0.8, "events": [PARENT, bad]},
            )
            attempt(
                "ordinary",
                "telegram:4403",
                4103,
                {"intent": "log", "confidence": 0.8, "events": [CHILD]},
            )
            output.write_text(
                json.dumps({"text": TEXT, "cases": observations}, ensure_ascii=False),
                encoding="utf-8",
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
