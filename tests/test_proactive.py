from datetime import UTC, datetime, timedelta

import pytest
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
    db.add(
        AppState(
            key="conversation:pending", value={"text": "synthetic", "created_at": now.isoformat()}
        )
    )
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
    db.add(AppState(key="conversation:pending", value={"text": "уточнение"}))
    db.flush()
    command = Interpretation(intent="acknowledge", target_question_id=q.id, confidence=1)
    response = apply_command(
        db, command, text="ещё продолжается", update_id=1, actor="owner", now=now
    )
    assert response and q.status == "acknowledged" and episode.end is None
    db.flush()
    assert db.get(AppState, "conversation:pending") is None
    episode.end = now
    db.flush()
    from garmin_ai.proactive import reconcile_answers

    reconcile_answers(db, now)
    assert q.status == "answered"


def test_all_unexpired_questions_remain_in_context(db):
    from garmin_ai.agent import context_for

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    for i in range(4):
        add_question(db, "context", "test", {}, 0.9, f"context-{i}", now)
    for q in db.scalars(select(PendingQuestion)):
        q.status, q.sent_at = "sent", now
    db.flush()
    assert len(context_for(db, now)["recent_questions"]) == 4


def test_absence_prevents_question_and_reply_window_starts_at_send(db):
    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    settings = Settings(proactive_enabled=True)
    for i in range(1, 9):
        create_event(
            db,
            EventInput(
                start=now - timedelta(days=i), payload={"type": "caffeine", "beverage": "coffee"}
            ),
            actor="owner",
        )
    create_event(
        db,
        EventInput(
            start=now.replace(hour=0),
            end=now,
            payload={"type": "caffeine_absence", "description": "none today"},
        ),
        actor="owner",
    )
    generate_questions(db, settings, now)
    assert db.scalar(select(func.count()).select_from(PendingQuestion)) == 0
    add_question(
        db, "context", "test", {}, 0.9, "delayed", now - timedelta(days=2) + timedelta(minutes=1)
    )
    q = select_question(db, settings, now)
    assert q.expires_at == now + timedelta(days=2)


def test_insight_cooldown_crosses_week_boundary(db):
    from garmin_ai.models import Insight
    from garmin_ai.proactive import generate_insights

    monday = datetime(2026, 9, 7, 12, tzinfo=UTC)
    db.add(
        AppState(
            key="insight:last:sleep_score", value={"at": (monday - timedelta(days=1)).isoformat()}
        )
    )
    db.flush()
    generate_insights(db, monday, "UTC")
    assert db.scalar(select(Insight).where(Insight.dedup_key.like("trend:sleep_score:%"))) is None


def test_acknowledgement_keeps_reported_severity(db):
    from garmin_ai.agent import Interpretation, apply_command

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    event = EventInput(start=now - timedelta(hours=3), payload={"type": "migraine", "severity": 5})
    episode = create_event(db, event, actor="owner")
    add_question(db, "migraine", "test", {}, 0.9, "compound", now, event_id=episode.id)
    q = db.scalar(select(PendingQuestion))
    q.status = "sent"
    q.sent_at = now
    proposed = EventInput(start=event.start, payload={"type": "migraine", "severity": 7})
    command = Interpretation(
        intent="acknowledge",
        target_question_id=q.id,
        events=[proposed],
        changed_fields=["payload.severity"],
        confidence=1,
    )
    apply_command(
        db, command, text="ещё продолжается, боль 7", update_id=10, actor="owner", now=now
    )
    assert episode.payload["severity"] == 7 and episode.end is None
    assert q.status == "acknowledged"


def test_followup_rejects_crossed_episode_targets(db):
    import pytest

    from garmin_ai.agent import Interpretation, apply_command

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    original = EventInput(
        start=now - timedelta(hours=3), payload={"type": "migraine", "severity": 5}
    )
    a = create_event(db, original, actor="owner")
    b = create_event(db, original, actor="owner")
    add_question(db, "migraine", "test", {}, 0.9, "crossed", now, event_id=a.id)
    q = db.scalar(select(PendingQuestion))
    command = Interpretation(
        intent="update",
        target_event_id=b.id,
        target_question_id=q.id,
        events=[original],
        changed_fields=["payload.severity"],
        confidence=1,
    )
    with pytest.raises(ValueError, match="same migraine"):
        apply_command(db, command, text="боль 7", update_id=1, actor="owner", now=now)
    assert b.revision == 1 and q.status == "pending"


def test_unconfirmed_medication_does_not_suppress_question(db):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    episode = create_event(
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
    )
    create_event(
        db,
        EventInput(
            start=now - timedelta(hours=2),
            status="needs_confirmation",
            payload={
                "type": "medication",
                "name": "synthetic",
                "dose": 1,
                "unit": "mg",
                "reason_event_id": episode.id,
            },
        ),
        actor="owner",
    )
    generate_questions(db, Settings(proactive_enabled=True), now)
    question = db.scalar(select(PendingQuestion))
    assert "приним" in question.text.lower()


def test_overlapping_context_questions_are_not_repeated(db):
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    for i in (0, 10):
        evidence = {
            "start": (now + timedelta(minutes=i)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        }
        add_question(db, "context", "test", evidence, 0.9, f"window-{i}", now)
    assert db.scalar(select(func.count()).select_from(PendingQuestion)) == 1


@pytest.mark.parametrize(
    "changed",
    [{"status": "needs_confirmation"}, {"payload": {"type": "note", "description": "corrected"}}],
)
def test_changed_episode_cancels_migraine_followup(db, changed):
    from garmin_ai.events import update_event
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    original = EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"})
    episode = create_event(db, original, actor="owner")
    add_question(db, "migraine", "test", {}, 0.9, "changed", now, event_id=episode.id)
    correction = EventInput.model_validate({**original.model_dump(), **changed})
    update_event(db, episode.id, correction, revision=1, actor="owner")
    assert select_question(db, Settings(proactive_enabled=True), now) is None
    q = db.scalar(select(PendingQuestion))
    assert q.status == "cancelled"
    q.status = "pending"
    db.flush()
    reconcile_answers(db, now)
    assert q.status == "cancelled"


def test_delayed_message_cannot_see_future_question(db):
    from garmin_ai.agent import context_for

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    add_question(db, "context", "future", {}, 0.9, "future", now)
    q = db.scalar(select(PendingQuestion))
    q.status = "sent"
    q.sent_at = now + timedelta(hours=1)
    db.flush()
    assert not context_for(db, now)["recent_questions"]


def test_medication_reply_cannot_acknowledge_another_migraine(db):
    from garmin_ai.agent import Interpretation, apply_command
    from garmin_ai.models import Event

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    a = create_event(db, EventInput(start=now, payload={"type": "migraine"}), actor="owner")
    b = create_event(db, EventInput(start=now, payload={"type": "migraine"}), actor="owner")
    add_question(db, "migraine", "test", {}, 0.9, "med-crossed", now, event_id=a.id)
    q = db.scalar(select(PendingQuestion))
    command = Interpretation(
        intent="log",
        confidence=1,
        target_question_id=q.id,
        events=[
            EventInput(
                start=now,
                payload={
                    "type": "medication",
                    "name": "synthetic",
                    "dose": 1,
                    "unit": "mg",
                    "reason_event_id": b.id,
                },
            )
        ],
    )
    with pytest.raises(ValueError, match="same migraine"):
        apply_command(db, command, text="лекарство", update_id=9, actor="owner", now=now)
    assert q.status == "pending"
    assert db.scalar(select(func.count()).select_from(Event).where(Event.kind == "medication")) == 0


@pytest.mark.parametrize("source", ["activity", "timeline", "event"])
def test_context_question_rechecks_late_explanations(db, source):
    from garmin_ai.models import Activity, TimelineInterval

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    left, right = now - timedelta(hours=1), now - timedelta(minutes=30)
    add_question(
        db,
        "context",
        "test",
        {"start": left.isoformat(), "end": right.isoformat()},
        0.9,
        "late-explanation",
        now,
    )
    if source == "activity":
        db.add(Activity(id=991, start=left, end=right, kind="running", timezone="UTC"))
    elif source == "timeline":
        db.add(
            TimelineInterval(
                id="synthetic",
                start=left,
                end=right,
                label="workout",
                evidence={},
                confidence=1,
                confirmed=True,
                source="synthetic",
            )
        )
    else:
        create_event(
            db,
            EventInput(
                start=left, end=right, payload={"type": "context", "description": "synthetic"}
            ),
            actor="owner",
        )
    db.flush()
    assert select_question(db, Settings(proactive_enabled=True), now) is None
    assert db.scalar(select(PendingQuestion)).status == "cancelled"


@pytest.mark.parametrize("outcome", ["done", "failed", "exhausted"])
def test_insight_claim_waits_for_scheduled_sync_without_spending_attempts(db, outcome):
    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    dependency = enqueue(db, "garmin_endpoint", {}, "synthetic-sync", now + timedelta(minutes=1))
    identity = enqueue(
        db, "agent_insights", {"sync_dependencies": [str(dependency)]}, "synthetic-insights", now
    )
    assert claim(db, now=now, kinds=["agent_insights"]) is None
    assert db.get(Job, identity).attempts == 0
    db.get(Job, dependency).status = outcome if outcome != "exhausted" else "pending"
    if outcome == "exhausted":
        db.get(Job, dependency).attempts = 8
    db.flush()
    assert claim(db, now=now, kinds=["agent_insights"]).id == identity


def test_analysis_tool_accepts_caffeine_absence(db):
    from garmin_ai.tools import call_tool

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    create_event(
        db,
        EventInput(
            start=now,
            end=now + timedelta(hours=1),
            payload={"type": "caffeine_absence", "description": "synthetic"},
        ),
        actor="owner",
    )
    result = call_tool(
        db,
        "analysis_event_windows",
        {
            "event_type": "caffeine_absence",
            "metric": "heart_rate_bpm",
            "start": now.isoformat(),
            "end": (now + timedelta(days=1)).isoformat(),
        },
    )
    assert result["episodes"] == 1 and result["event_type"] == "caffeine_absence"


@pytest.mark.parametrize("sent", [False, True])
def test_cancelled_migraine_followup_returns_when_episode_is_restored(db, sent):
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    episode = create_event(
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
    )
    generate_questions(db, Settings(), now)
    question = db.scalar(select(PendingQuestion))
    if sent:
        question.status, question.sent_at = "sent", now - timedelta(minutes=1)
    episode.status = "needs_confirmation"
    db.flush()
    reconcile_answers(db, now)
    assert question.status == "cancelled"
    episode.status = "confirmed"
    db.flush()
    reconcile_answers(db, now)
    assert question.status == ("sent" if sent else "pending")
    assert db.scalar(select(func.count()).select_from(PendingQuestion)) == 1


@pytest.mark.parametrize("kind", ["caffeine", "caffeine_absence", "context"])
def test_linked_non_migraine_reply_persists_evidence_and_answers_question(db, kind):
    from garmin_ai.agent import Interpretation, apply_command

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    category = "context" if kind == "context" else "caffeine"
    evidence = (
        {
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        }
        if category == "context"
        else {"day": str(now.date()), "timezone": "UTC"}
    )
    add_question(db, category, "synthetic", evidence, 0.9, "linked-answer", now)
    q = db.scalar(select(PendingQuestion))
    q.status, q.sent_at = "sent", now
    payload = (
        {"type": "caffeine", "beverage": "synthetic"}
        if kind == "caffeine"
        else {"type": kind, "description": "synthetic"}
    )
    command = Interpretation(
        intent="log",
        confidence=1,
        target_question_id=q.id,
        events=[EventInput(start=now, payload=payload)],
    )
    apply_command(db, command, text="synthetic", update_id=1, actor="owner", now=now)
    assert q.status == "answered"


def test_zero_variance_shift_can_be_an_accepted_trend(db):
    from garmin_ai.models import HealthDay, Insight
    from garmin_ai.proactive import generate_insights

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    for offset in range(1, 29):
        db.add(
            HealthDay(
                day=now.date() - timedelta(days=offset), resting_hr=70 if offset <= 14 else 60
            )
        )
    db.flush()
    generate_insights(db, now, "UTC")
    row = db.scalar(select(Insight).where(Insight.dedup_key.like("trend:resting_hr:%")))
    assert row.status == "accepted" and row.effect_size is None
    assert row.evidence["difference"] == 10


def test_future_coffee_cannot_create_or_suppress_habit_question(db):
    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    for offset in range(1, 8):
        create_event(
            db,
            EventInput(
                start=now + timedelta(days=offset),
                payload={"type": "caffeine", "beverage": "synthetic"},
            ),
            actor="owner",
        )
    settings = Settings(timezone="UTC")
    generate_questions(db, settings, now)
    assert db.scalar(select(func.count()).select_from(PendingQuestion)) == 0
    for offset in range(1, 8):
        create_event(
            db,
            EventInput(
                start=now - timedelta(days=offset),
                payload={"type": "caffeine", "beverage": "synthetic"},
            ),
            actor="owner",
        )
    create_event(
        db,
        EventInput(
            start=now + timedelta(hours=3), payload={"type": "caffeine", "beverage": "synthetic"}
        ),
        actor="owner",
    )
    generate_questions(db, settings, now + timedelta(minutes=31))
    assert db.scalar(select(PendingQuestion)).kind == "caffeine"


def test_proactive_job_waits_for_due_activity_pages(db):
    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    activity = enqueue(db, "garmin_activities", {}, "synthetic-page", now)
    proactive = enqueue(db, "agent_proactive", {}, "synthetic-proactive", now)
    assert claim(db, now=now, kinds=["agent_proactive"]) is None
    db.get(Job, activity).status = "done"
    db.flush()
    assert claim(db, now=now, kinds=["agent_proactive"]).id == proactive


@pytest.mark.parametrize("end_day,answered", [(7, True), (6, False)])
def test_multiday_caffeine_absence_overlaps_question_day(db, end_day, answered):
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    add_question(
        db,
        "caffeine",
        "test",
        {"day": "2026-09-07", "timezone": "UTC"},
        1,
        "synthetic-multiday",
        now,
    )
    create_event(
        db,
        EventInput(
            start=now - timedelta(days=2),
            end=now.replace(day=end_day),
            payload={"type": "caffeine_absence", "description": "synthetic absence"},
        ),
        actor="owner",
    )
    reconcile_answers(db, now)
    assert db.scalar(select(PendingQuestion)).status == ("answered" if answered else "pending")


def test_multiday_absence_prevents_redundant_question(db):
    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    settings = Settings(timezone="UTC", proactive_enabled=True)
    for i in range(1, 8):
        create_event(
            db,
            EventInput(
                start=now - timedelta(days=i), payload={"type": "caffeine", "beverage": "synthetic"}
            ),
            actor="owner",
        )
    create_event(
        db,
        EventInput(
            start=now - timedelta(days=2),
            end=now,
            payload={"type": "caffeine_absence", "description": "synthetic absence"},
        ),
        actor="owner",
    )
    generate_questions(db, settings, now)
    assert db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "caffeine")) is None


def test_pending_pause_control_blocks_insight_notifications(db):
    from garmin_ai.models import TelegramUpdate
    from garmin_ai.proactive import can_notify
    from garmin_ai.telegram import save_update

    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    settings = Settings(timezone="UTC", proactive_enabled=True)
    assert can_notify(db, settings, now)
    save_update(
        db,
        {
            "update_id": 777,
            "message": {
                "message_id": 777,
                "date": int(now.timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/pause",
            },
        },
        42,
    )
    assert not can_notify(db, settings, now)
    db.get(TelegramUpdate, 777).status = "processed"
    db.flush()
    assert can_notify(db, settings, now)
