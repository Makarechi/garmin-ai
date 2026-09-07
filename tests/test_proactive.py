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


def test_unsent_question_recovery_and_pending_clarification(db):
    from garmin_ai.proactive import reconcile_questions

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    settings = Settings(proactive_enabled=True)
    add_question(db, "context", "test", {}, 0.9, "recovery", now)
    first = select_question(db, settings, now)
    assert first.status == "sending"
    reconcile_questions(db)
    assert first.status == "pending" and first.sent_at is None
    db.add(AppState(key="conversation:pending", value={"text": "synthetic"}))
    db.flush()
    assert select_question(db, settings, now) is None


def test_candidates_recompute_after_missing_days_arrive(db):
    from garmin_ai.models import HealthDay, Insight
    from garmin_ai.proactive import generate_insights

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    generate_insights(db, now, "UTC")
    assert (
        db.scalar(select(Insight).where(Insight.dedup_key.like("trend:sleep_score:%"))).status
        == "candidate"
    )
    for i in range(1, 29):
        db.add(
            HealthDay(
                day=now.date() - timedelta(days=i), sleep_score=(80 if i <= 14 else 50) + i % 3
            )
        )
    db.flush()
    generate_insights(db, now + timedelta(hours=6), "UTC")
    db.expire_all()
    insight = db.scalar(select(Insight).where(Insight.dedup_key.like("trend:sleep_score:%")))
    assert insight.status == "accepted" and insight.sample_size == 28


def test_answers_cancel_pending_and_undo_restores_context(db):
    from garmin_ai.events import undo_last
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    add_question(
        db, "caffeine", "test", {"day": "2026-09-07", "timezone": "UTC"}, 0.9, "coffee-day", now
    )
    create_event(
        db,
        EventInput(start=now, payload={"type": "caffeine", "beverage": "synthetic"}),
        actor="owner",
    )
    reconcile_answers(db, now)
    question = db.scalar(select(PendingQuestion))
    assert question.status == "answered"
    undo_last(db, actor="owner")
    reconcile_answers(db, now)
    assert question.status == "pending"
    create_event(
        db,
        EventInput(
            start=now.replace(hour=0),
            end=now,
            payload={"type": "caffeine_absence", "description": "synthetic absence"},
        ),
        actor="owner",
    )
    reconcile_answers(db, now)
    assert question.status == "answered"


def test_uncertain_questions_keep_target_in_context(db):
    from garmin_ai.agent import context_for

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    episode = create_event(
        db, EventInput(start=now - timedelta(days=3), payload={"type": "migraine"}), actor="owner"
    )
    for i in range(15):
        create_event(
            db,
            EventInput(
                start=now - timedelta(minutes=i),
                payload={"type": "note", "description": "synthetic"},
            ),
            actor="owner",
        )
    add_question(
        db,
        "migraine",
        "test",
        {"event_id": str(episode.id)},
        0.9,
        "old-episode",
        now,
        event_id=episode.id,
    )
    q = db.scalar(select(PendingQuestion))
    q.status, q.sent_at = "uncertain", now
    db.flush()
    context = context_for(db, now)
    assert str(episode.id) in {e["id"] for e in context["recent_events"]}
    assert context["recent_questions"][0]["status"] == "uncertain"


def test_negative_migraine_reply_acknowledges_without_closing(db):
    from garmin_ai.agent import Interpretation, apply_command

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    episode = create_event(
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
    )
    add_question(
        db,
        "migraine",
        "test",
        {"event_id": str(episode.id)},
        0.9,
        "ongoing",
        now,
        event_id=episode.id,
    )
    q = db.scalar(select(PendingQuestion))
    q.status, q.sent_at = "sent", now
    command = Interpretation(intent="acknowledge", target_question_id=q.id, confidence=1)
    response = apply_command(
        db, command, text="ещё продолжается", update_id=1, actor="owner", now=now
    )
    assert response and q.status == "acknowledged" and episode.end is None


def test_all_unexpired_questions_remain_in_context(db):
    from garmin_ai.agent import context_for

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    for i in range(4):
        add_question(db, "context", "test", {}, 0.9, f"context-{i}", now)
    for q in db.scalars(select(PendingQuestion)):
        q.status, q.sent_at = "sent", now
    db.flush()
    assert len(context_for(db, now)["recent_questions"]) == 4
