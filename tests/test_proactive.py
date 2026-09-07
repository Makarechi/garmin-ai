from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState, PendingQuestion
from garmin_ai.proactive import add_question, can_notify, generate_questions, select_question


def test_migraine_followup_dedup_quiet_hours_and_pause(db):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    settings = Settings(proactive_enabled=True)
    event = EventInput(start=now - timedelta(hours=3), payload={"type": "migraine", "severity": 6})
    create_event(db, event, actor="owner")
    generate_questions(db, settings, now)
    generate_questions(db, settings, now + timedelta(minutes=31))
    assert db.scalar(select(func.count()).select_from(PendingQuestion)) == 1
    assert select_question(db, settings, now.replace(hour=23)) is None
    q = select_question(db, settings, now)
    assert q and q.kind == "migraine" and q.attempts == 1
    assert select_question(db, settings, now + timedelta(hours=2)) is None
    db.add(AppState(key="proactive:enabled", value={"enabled": False}))
    db.flush()
    assert can_notify(db, settings, now) is False


def test_question_budget_and_category_cooldown(db):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    settings = Settings(proactive_enabled=True, question_budget=2)
    for category in ["one", "one", "two", "three"]:
        add_question(
            db,
            category,
            "test",
            {},
            0.9,
            f"{category}:{db.scalar(select(func.count()).select_from(PendingQuestion))}",
            now,
        )
        db.flush()
    first = select_question(db, settings, now)
    assert first is not None
    second = select_question(db, settings, now)
    assert second is not None and second.kind != first.kind
    assert select_question(db, settings, now) is None
