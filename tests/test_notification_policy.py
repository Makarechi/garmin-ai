from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState, Insight, PendingQuestion
from garmin_ai.proactive import (
    add_question,
    generate_questions,
    pending_insight_notices,
    reserve_insight_notice,
    select_question,
)

NOW = datetime(2026, 9, 10, 16, tzinfo=UTC)
SETTINGS = Settings(proactive_enabled=True, timezone="UTC", question_budget=2)


def insight(metric):
    return SimpleNamespace(id=uuid4(), dedup_key=f"trend:{metric}:synthetic")


def test_question_and_insight_share_budget_across_restart(db):
    add_question(db, "synthetic", "test", {}, 1, "q1", NOW)
    assert select_question(db, SETTINGS, NOW)
    first = insight("sleep_seconds")
    assert reserve_insight_notice(db, SETTINGS, NOW, first)
    db.commit()
    db.expire_all()
    assert not reserve_insight_notice(db, SETTINGS, NOW, insight("resting_hr"))
    # The same accepted insight can resume its rate-limited outbox delivery.
    assert reserve_insight_notice(db, SETTINGS, NOW, first)
    add_question(db, "other", "test", {}, 1, "q2", NOW)
    assert select_question(db, SETTINGS, NOW) is None


def test_two_insights_block_question_and_third_insight(db):
    assert reserve_insight_notice(db, SETTINGS, NOW, insight("sleep_seconds"))
    assert reserve_insight_notice(db, SETTINGS, NOW, insight("resting_hr"))
    assert not reserve_insight_notice(db, SETTINGS, NOW, insight("hrv_nightly_avg"))
    add_question(db, "synthetic", "test", {}, 1, "q", NOW)
    assert select_question(db, SETTINGS, NOW) is None


@pytest.mark.parametrize(
    "status,expected",
    [("answered", True), ("acknowledged", True), ("sent", False), ("uncertain", False)],
)
def test_answered_coffee_question_is_not_a_week_of_ignoring(db, status, expected):
    for i in range(1, 8):
        create_event(
            db,
            EventInput(
                start=NOW - timedelta(days=i),
                timezone="UTC",
                payload={"type": "caffeine", "beverage": "synthetic"},
            ),
            actor="test",
        )
    add_question(db, "caffeine", "test", {}, 1, "previous", NOW - timedelta(days=2))
    previous = db.scalar(select(PendingQuestion))
    previous.status = status
    previous.sent_at = NOW - timedelta(days=2)
    db.flush()
    generate_questions(db, SETTINGS, NOW, allow_context=False)
    assert (
        bool(
            db.scalar(
                select(PendingQuestion).where(PendingQuestion.dedup_key == f"caffeine:{NOW.date()}")
            )
        )
        == expected
    )


def test_pause_and_zero_budget_apply_to_insights(db):
    assert not reserve_insight_notice(
        db,
        Settings(proactive_enabled=True, timezone="UTC", question_budget=0),
        NOW,
        insight("sleep_seconds"),
    )
    db.add(AppState(key="proactive:enabled", value={"enabled": False}))
    db.commit()
    db.expire_all()
    assert not reserve_insight_notice(db, SETTINGS, NOW, insight("sleep_seconds"))


def test_reserved_insight_survives_long_pause_and_prioritizes_retry(db):
    original = Insight(
        category="trend",
        statement="synthetic",
        evidence={},
        sample_size=28,
        dedup_key="trend:sleep_seconds:old",
        status="accepted",
        generated_at=NOW,
    )
    db.add(original)
    db.flush()
    assert reserve_insight_notice(db, SETTINGS, NOW, original)
    identity = original.id
    later = NOW + timedelta(days=2)
    for index in range(4):
        db.add(
            Insight(
                category="trend",
                statement="synthetic",
                evidence={},
                sample_size=28,
                dedup_key=f"trend:synthetic:{index}",
                status="accepted",
                generated_at=later,
            )
        )
    db.commit()
    db.expire_all()
    pending = pending_insight_notices(db, later)
    assert len(pending) == 3
    assert pending[0].id == identity
    assert reserve_insight_notice(db, SETTINGS, later, pending[0])
    pending[0].status = "delivered"
    db.flush()
    assert identity not in [item.id for item in pending_insight_notices(db, later)]
