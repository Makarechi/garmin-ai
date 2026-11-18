"""Capture diary question generation with the pinned Python runtime."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState, PendingQuestion
from garmin_ai.proactive import generate_questions
from garmin_ai.scenario_packs import ensure_scenario_packs

NOW = datetime(2026, 10, 25, 14, 30, tzinfo=UTC)
ZONE = "Europe/Bratislava"


def seed(session):
    person = owner(session)
    person.timezone = ZONE
    session.add(AppState(key="preferences:onboarding", value={"synthetic": True}))
    packs = ensure_scenario_packs(session, legacy_install=False)
    for key in ("migraine", "caffeine"):
        packs[key].tracking_enabled = packs[key].reminders_enabled = True
    session.flush()

    def record(start, payload, end=None):
        return create_event(
            session,
            EventInput(start=start, end=end, timezone=ZONE, payload=payload),
            actor="fixture",
        )

    record(NOW - timedelta(hours=3), {"type": "migraine"})
    treated = record(NOW - timedelta(hours=2), {"type": "migraine"})
    record(
        NOW - timedelta(minutes=90),
        {"type": "medication", "name": "synthetic", "reason_event_id": treated.id},
    )
    record(
        NOW - timedelta(minutes=80),
        {"type": "symptom_observation", "episode_id": treated.id, "severity": 5},
    )
    waiting = record(NOW - timedelta(minutes=90), {"type": "migraine", "severity": 7})
    record(NOW - timedelta(days=2, seconds=1), {"type": "migraine"})
    record(NOW - timedelta(hours=6), {"type": "migraine"}, end=NOW - timedelta(hours=4))
    for day in range(1, 8):
        record(
            NOW - timedelta(days=day),
            {"type": "caffeine", "beverage": "synthetic", "caffeine_mg_estimate": 30},
        )
    next_day = NOW + timedelta(days=1)
    record(
        next_day.replace(hour=0, minute=0) - timedelta(hours=1),
        {"type": "caffeine_absence", "description": "synthetic absence"},
        end=next_day,
    )
    session.flush()
    return person.id, waiting.id


def capture(path):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            person, waiting = seed(session)
            settings = Settings(_env_file=None, timezone="UTC")
            generate_questions(
                session, settings, NOW, allow_context=False, include_tracker_checkins=False
            )
            rows = session.scalars(
                select(PendingQuestion).order_by(PendingQuestion.dedup_key)
            ).all()
            assert [row.kind for row in rows] == ["caffeine", "migraine", "migraine"]
            text = {row.text.split("?")[0]: row.text for row in rows if row.kind == "migraine"}
            assert len(text) == 2
            assert sum("Принимали" in body for body in text.values()) == 1
            assert sum("Можно также указать силу" in body for body in text.values()) == 1
            expected = [
                dict(
                    kind=row.kind,
                    key=row.dedup_key,
                    text=row.text,
                    evidence=row.evidence,
                    priority=row.priority,
                    status=row.status,
                    attempts=row.attempts,
                    event_id=None if row.event_id is None else str(row.event_id),
                    earliest=row.earliest_send_at.isoformat(),
                    expires=row.expires_at.isoformat(),
                    sent_at=row.sent_at,
                )
                for row in rows
            ]
            slot = session.get(AppState, "proactive:generation")
            assert slot.value["context_complete"] is False
            generation = dict(slot.value)
            generate_questions(
                session,
                settings,
                NOW + timedelta(days=1),
                allow_context=False,
                include_tracker_checkins=False,
            )
            caffeinated = session.scalars(
                select(PendingQuestion.dedup_key).where(PendingQuestion.kind == "caffeine")
            ).all()
            assert caffeinated == [f"caffeine:{NOW.date()}"]
            session.execute(delete(PendingQuestion))
            session.delete(slot)
        path.write_text(
            json.dumps(
                dict(
                    owner=str(person),
                    waiting=str(waiting),
                    now=NOW.isoformat(),
                    questions=expected,
                    generation=generation,
                ),
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
