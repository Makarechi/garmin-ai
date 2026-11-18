"""Capture the existing selection, budget and revised diary evidence on synthetic rows."""

import argparse
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from capture_reference import isolated_engine
from check_diary_question_generation import NOW, seed
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.config import Settings
from garmin_ai.models import Event, PendingQuestion
from garmin_ai.proactive import add_question, generate_questions, select_question


def state(session):
    return {
        str(row.id): {
            "status": row.status,
            "attempt": row.attempts,
            "sent": row.sent_at.isoformat() if row.sent_at else None,
            "expires": row.expires_at.isoformat(),
            "text": row.text,
        }
        for row in session.scalars(select(PendingQuestion))
    }


def capture(path):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            person, _waiting = seed(session)
            settings = Settings(_env_file=None, timezone="UTC", proactive_enabled=True)
            generate_questions(
                session, settings, NOW, allow_context=False, include_tracker_checkins=False
            )
            for item in session.scalars(
                select(PendingQuestion).where(PendingQuestion.kind == "migraine")
            ):
                if "Принимали" in item.text:
                    event = session.get(Event, item.event_id)
                    event.end = NOW - timedelta(hours=1)
                    event.topology = "bounded_interval"
                    item.priority = 0.98
            for event in session.scalars(
                select(Event).where(Event.kind.in_(["medication", "symptom_observation"]))
            ):
                event.deleted = True
            add_question(
                session,
                "migraine",
                "Obsolete synthetic question",
                {},
                0.99,
                "fixture:missing-episode",
                NOW,
                event_id=uuid4(),
            )
            session.flush()
            before = state(session)
            results = []
            for _ in range(3):
                row = select_question(session, settings, NOW, allow_context=False)
                session.flush()
                results.append(
                    {
                        "id": str(row.id),
                        "kind": row.kind,
                        "text": row.text,
                        "event_id": str(row.event_id) if row.event_id else None,
                        "attempt": row.attempts,
                        "reserved_at": row.sent_at.isoformat(),
                    }
                    if row
                    else None
                )
            assert [choice["kind"] if choice else None for choice in results] == [
                "migraine",
                "caffeine",
                None,
            ]
            assert "Принимали" in results[0]["text"] and "Можно также" in results[0]["text"]
            observed = state(session)
            assert sorted(row["status"] for row in observed.values()) == [
                "answered",
                "cancelled",
                "sending",
                "sending",
            ]
            for row in session.scalars(select(PendingQuestion)):
                expected = before[str(row.id)]
                row.status = expected["status"]
                row.text = expected["text"]
                row.attempts = 0
                row.sent_at = None
                row.expires_at = NOW + timedelta(days=2)
        path.write_text(
            json.dumps(
                dict(owner=str(person), now=NOW.isoformat(), results=results, state=observed),
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
