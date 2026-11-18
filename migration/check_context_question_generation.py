"""Capture context question generation with the pinned Python runtime."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner
from garmin_ai.config import Settings
from garmin_ai.models import AppState, Measurement, PendingQuestion, TimelineInterval
from garmin_ai.proactive import generate_questions, select_question
from garmin_ai.scenario_packs import ensure_scenario_packs

NOW = datetime(2026, 10, 26, 0, 35, tzinfo=UTC)
ZONE = "Europe/Bratislava"


def seed(session):
    person = owner(session)
    person.timezone = ZONE
    session.add(AppState(key="preferences:onboarding", value={"synthetic": True}))
    packs = ensure_scenario_packs(session, legacy_install=False)
    packs["wellbeing"].reminders_enabled = True
    packs["general_diary"].reminders_enabled = True
    packs["general_diary"].tracking_enabled = True

    def sample(ts, metric, value, unit):
        session.add(Measurement(ts=ts, local_date=ts.date(), metric=metric, value=value, unit=unit))

    def window(left, source, start, end):
        session.add(
            TimelineInterval(
                id=f"synthetic-{left.isoformat()}-{source}",
                start=left + timedelta(minutes=start),
                end=left + timedelta(minutes=end),
                source=source,
                label="Synthetic context",
                confidence=1,
                confirmed=True,
                evidence={},
            )
        )

    for day in range(2, 10):
        for minute in range(30):
            sample(
                NOW - timedelta(days=day, minutes=minute), "heart_rate_bpm", 60 + minute % 5, "bpm"
            )
    for offset in (110, 70, 36):
        left = NOW - timedelta(minutes=offset)
        for minute in range(0, 24 if offset != 36 else 22, 2):
            ts = left + timedelta(minutes=minute)
            sample(ts, "stress_score", 85, "score")
            sample(ts, "heart_rate_bpm", 95 if offset != 36 else 50, "bpm")
        if offset == 36:
            sample(left + timedelta(minutes=21), "stress_score", 85, "score")
        elif offset == 70:
            window(left, "manual", 0, 24)
        else:
            window(left, "manual", 4, 10)
            window(left, "external_calendar", 0, 24)
    session.flush()
    return person.id


def snapshot(row):
    return dict(
        key=row.dedup_key,
        text=row.text,
        evidence=row.evidence,
        priority=row.priority,
        status=row.status,
        earliest=row.earliest_send_at.isoformat(),
        expires=row.expires_at.isoformat(),
    )


def capture(path):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            person = seed(session)
            settings = Settings(
                _env_file=None,
                timezone="UTC",
                proactive_enabled=True,
                quiet_start_hour=0,
                quiet_end_hour=0,
            )

            def invoke(at, enabled):
                generate_questions(
                    session, settings, at, allow_context=enabled, include_tracker_checkins=False
                )

            questions = select(PendingQuestion).where(PendingQuestion.kind == "context")
            invoke(NOW, False)
            assert session.scalar(questions) is None
            state = session.get(AppState, "proactive:generation")
            assert state.value["context_complete"] is False
            invoke(NOW, True)
            row = session.scalars(questions).one()
            generated = snapshot(row)
            assert row.text.startswith("25.10.2026 с 23:45 до 26.10.2026 00:09")
            assert row.evidence["context_coverage"]["covered_seconds"] == 360
            assert row.evidence["baseline_hr_p95"] == 64
            invoke(NOW + timedelta(minutes=30), True)
            assert snapshot(session.scalars(questions).one()) == generated
            row.status = "cancelled"
            row.evidence = {**row.evidence, "cancel_reason": "synthetic-replay"}
            selected_at = NOW + timedelta(minutes=60)
            invoke(selected_at, True)
            reused = snapshot(session.scalars(questions).one())
            assert reused["status"] == "pending" and reused["key"] == generated["key"]
            selected = snapshot(select_question(session, settings, selected_at))
            assert selected["status"] == "sending"
            assert "Неизвестный контекст: 18 мин" in selected["text"]
            session.execute(delete(PendingQuestion))
            session.delete(state)
        path.write_text(
            json.dumps(
                dict(
                    owner=str(person),
                    now=NOW.isoformat(),
                    generated=generated,
                    reused=reused,
                    selected=selected,
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
