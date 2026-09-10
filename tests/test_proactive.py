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
    assert db.scalar(select(Insight).where(Insight.dedup_key.like("trend:sleep_score:%"))) is None
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
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
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
    settings = Settings(proactive_enabled=True, timezone="UTC")
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
        events=[
            EventInput(
                start=now.replace(hour=0) if kind == "caffeine_absence" else now,
                end=now if kind == "caffeine_absence" else None,
                payload=payload,
            )
        ],
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


@pytest.mark.parametrize(
    "start_hour,end_hour,answered", [(0, 16, True), (8, 16, False), (0, 12, False), (8, 12, False)]
)
def test_partial_absence_does_not_resolve_whole_day(db, start_hour, end_hour, answered):
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    add_question(
        db, "caffeine", "test", {"day": "2026-09-07", "timezone": "UTC"}, 1, "partial-day", now
    )
    create_event(
        db,
        EventInput(
            start=now.replace(hour=start_hour),
            end=now.replace(hour=end_hour),
            payload={"type": "caffeine_absence", "description": "synthetic"},
        ),
        actor="owner",
    )
    reconcile_answers(db, now + timedelta(hours=1))
    assert db.scalar(select(PendingQuestion)).status == ("answered" if answered else "pending")


def test_future_medication_does_not_count_as_taken(db):
    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    episode = create_event(
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
    )
    create_event(
        db,
        EventInput(
            start=now + timedelta(hours=1),
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
    generate_questions(db, Settings(), now)
    assert "Принимали ли" in db.scalar(select(PendingQuestion)).text


@pytest.mark.parametrize("endpoint", ["heart_rate", "stress"])
def test_proactive_waits_for_metric_sync(db, endpoint):
    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    metric = enqueue(db, "garmin_endpoint", {"endpoint": endpoint}, "synthetic-metric", now)
    proactive = enqueue(db, "agent_proactive", {}, "synthetic-proactive", now)
    assert claim(db, now=now, kinds=["agent_proactive"]) is None
    db.get(Job, metric).status = "done"
    db.flush()
    assert claim(db, now=now, kinds=["agent_proactive"]).id == proactive


def test_long_followup_answers_do_not_overflow_prompt(db):
    import json

    from garmin_ai.agent import Interpretation, interpret

    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    for i in range(5):
        db.add(
            PendingQuestion(
                kind="migraine",
                text="synthetic",
                evidence={"answer_text": "x" * 15000, "status": "unknown"},
                priority=1,
                earliest_send_at=now,
                sent_at=now,
                expires_at=now + timedelta(days=1),
                status="acknowledged",
                dedup_key=f"long:{i}",
            )
        )
    db.flush()

    class Provider:
        def structured(self, instruction, prompt, schema):
            value = json.loads(prompt)
            assert len(prompt) < 24000 and len(value["context"]["recent_questions"]) == 5
            assert all(
                q["id"] and q["kind"] == "migraine" for q in value["context"]["recent_questions"]
            )
            return Interpretation(
                intent="clarify", confidence=1, clarification="synthetic clarification"
            )

    assert (
        interpret(db, Provider(), "x" * 15000, Settings(), now).clarification
        == "synthetic clarification"
    )


def test_context_prompt_displays_both_dates_across_midnight(db):
    from garmin_ai.models import Measurement

    now = datetime(2026, 9, 8, 0, 40, tzinfo=UTC)
    for day in range(1, 8):
        for minute in range(30):
            ts = now - timedelta(days=day, minutes=minute)
            db.add(
                Measurement(
                    ts=ts,
                    metric="heart_rate_bpm",
                    source="synthetic",
                    local_date=ts.date(),
                    value=60,
                    unit="bpm",
                )
            )
    for minute in range(31):
        ts = now - timedelta(minutes=50 - minute)
        for metric, value, unit in [("heart_rate_bpm", 100, "bpm"), ("stress_score", 90, "score")]:
            db.add(
                Measurement(
                    ts=ts,
                    metric=metric,
                    source="synthetic",
                    local_date=ts.date(),
                    value=value,
                    unit=unit,
                )
            )
    db.flush()
    generate_questions(db, Settings(timezone="UTC"), now)
    question = db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "context"))
    assert question is not None
    assert "07.09.2026" in question.text and "08.09.2026" in question.text


def test_acknowledged_correction_preserves_observed_revision(db, db_engine):
    from garmin_ai.agent import Interpretation, apply_command, interpret
    from garmin_ai.db import transaction
    from garmin_ai.events import Conflict, update_event
    from garmin_ai.models import Event

    now = datetime.now(UTC)
    event = EventInput(start=now - timedelta(hours=3), payload={"type": "migraine", "severity": 3})
    row = create_event(db, event, actor="owner")
    identity = row.id
    add_question(db, "migraine", "synthetic", {}, 1, "concurrent-ack", now, event_id=identity)
    q = db.scalar(select(PendingQuestion))
    q.status = "sent"
    q.sent_at = now
    question_id = q.id
    db.commit()

    class Provider:
        def structured(self, *args):
            with transaction(db_engine) as other:
                target = other.get(Event, identity)
                update_event(
                    other,
                    identity,
                    EventInput(start=event.start, payload={"type": "migraine", "severity": 7}),
                    revision=target.revision,
                    actor="api",
                )
            return Interpretation(
                intent="acknowledge",
                confidence=1,
                target_question_id=question_id,
                events=[EventInput(start=event.start, payload={"type": "migraine", "severity": 4})],
                changed_fields=["payload.severity"],
            )

    command = interpret(
        db, Provider(), "всё ещё болит, сила четыре", Settings(), now, before_model=db.commit
    )
    with pytest.raises(Conflict):
        apply_command(db, command, text="synthetic", update_id=777, actor="owner", now=now)
    db.rollback()
    db.expire_all()
    assert db.get(Event, identity).payload["severity"] == 7


def test_proactive_waits_for_randomly_delayed_cycle_metrics(db, monkeypatch):
    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.models import Job
    from garmin_ai.sync import schedule_sync

    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    monkeypatch.setattr("garmin_ai.sync.random.uniform", lambda *args: 60)
    schedule_sync(db, Settings(timezone="UTC"), now)
    proactive = enqueue(db, "agent_proactive", {}, "synthetic-cycle", now)
    metric_jobs = list(
        db.scalars(
            select(Job).where(
                (Job.kind == "garmin_activities")
                | (
                    (Job.kind == "garmin_endpoint")
                    & Job.payload["endpoint"].as_string().in_(["heart_rate", "stress"])
                )
            )
        )
    )
    assert {j.payload.get("endpoint") for j in metric_jobs if j.run_at > now} == {
        "heart_rate",
        "stress",
    }
    for job in metric_jobs:
        if job.kind == "garmin_activities":
            job.status = "done"
    db.flush()
    assert claim(db, now=now, kinds=["agent_proactive"]) is None
    for job in metric_jobs:
        job.status = "done"
    db.flush()
    assert claim(db, now=now, kinds=["agent_proactive"]).id == proactive


@pytest.mark.parametrize("future_medication", [False, True])
def test_delayed_migraine_prompt_refreshes_current_details(db, future_medication):
    from garmin_ai.events import update_event

    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    settings = Settings(timezone="UTC", proactive_enabled=True)
    episode = create_event(
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
    )
    generate_questions(db, settings, now)
    question = db.scalar(select(PendingQuestion))
    assert "Принимали ли" in question.text and "силу боли" in question.text
    update_event(
        db,
        episode.id,
        EventInput(start=episode.start, payload={"type": "migraine", "severity": 5}),
        revision=episode.revision,
        actor="owner",
    )
    create_event(
        db,
        EventInput(
            start=now + timedelta(hours=2) if future_medication else now + timedelta(minutes=30),
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
    selected = select_question(db, settings, now + timedelta(hours=1))
    assert selected.id == question.id and "силу боли" not in selected.text
    assert ("Принимали ли" in selected.text) == future_medication


@pytest.mark.parametrize("status", ["answered", "cancelled"])
@pytest.mark.parametrize("sent", [False, True])
def test_expired_restored_followup_is_available_without_resending(db, status, sent):
    from garmin_ai.agent import context_for
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 7, 16, tzinfo=UTC)
    episode = create_event(
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
    )
    q = PendingQuestion(
        kind="migraine",
        event_id=episode.id,
        text="synthetic",
        evidence={},
        priority=1,
        earliest_send_at=now - timedelta(days=4),
        expires_at=now - timedelta(days=1),
        sent_at=now - timedelta(days=3) if sent else None,
        status=status,
        dedup_key="expired-restored",
    )
    db.add(q)
    db.flush()
    reconcile_answers(db, now)
    expiry = q.expires_at
    assert expiry == now + timedelta(days=2)
    if sent:
        assert q.status == "sent" and q.sent_at == now - timedelta(days=3)
        assert str(q.id) in {r["id"] for r in context_for(db, now)["recent_questions"]}
        assert select_question(db, Settings(timezone="UTC", proactive_enabled=True), now) is None
    else:
        assert select_question(db, Settings(timezone="UTC", proactive_enabled=True), now).id == q.id
    reconcile_answers(db, now + timedelta(hours=1))
    assert q.expires_at == expiry


@pytest.mark.parametrize("mutation", ["deleted", "kind", "status", "closed", "future"])
def test_acknowledgement_revalidates_current_episode(db, mutation):
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
        "stale-question",
        now,
        event_id=episode.id,
    )
    q = db.scalar(select(PendingQuestion))
    q.status, q.sent_at = "sent", now
    if mutation == "deleted":
        episode.deleted = True
    elif mutation == "kind":
        episode.kind = "note"
        episode.payload = {"type": "note", "description": "synthetic"}
    elif mutation == "status":
        episode.status = "needs_confirmation"
    elif mutation == "closed":
        episode.end = now
    else:
        episode.start = now + timedelta(hours=1)
    db.flush()
    response = apply_command(
        db,
        Interpretation(intent="acknowledge", target_question_id=q.id, confidence=1),
        text="ещё продолжается",
        update_id=60,
        actor="owner",
        now=now,
    )
    assert q.status in {"cancelled", "answered"} and "Уточните" in response
    assert "answer_text" not in q.evidence


@pytest.mark.parametrize("source", ["activity", "timeline"])
def test_sent_context_prompt_retired_when_late_evidence_arrives(db, source):
    from garmin_ai.agent import context_for
    from garmin_ai.models import Activity, TimelineInterval

    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    left, right = now - timedelta(hours=1), now - timedelta(minutes=30)
    add_question(
        db,
        "context",
        "test",
        {"start": left.isoformat(), "end": right.isoformat()},
        0.9,
        "late-sent",
        now,
    )
    q = db.scalar(select(PendingQuestion))
    q.status, q.sent_at = "sent", now
    if source == "activity":
        db.add(Activity(id=992, start=left, end=right, kind="running", timezone="UTC"))
    else:
        db.add(
            TimelineInterval(
                id="late-timeline",
                start=left,
                end=right,
                label="workout",
                evidence={},
                confidence=1,
                confirmed=True,
                source="synthetic",
            )
        )
    db.flush()
    context = context_for(db, now)
    assert q.status == "cancelled" and context["recent_questions"] == []


def seed_context_measurements(db, now):
    from garmin_ai.models import Measurement

    for day in range(1, 8):
        for minute in range(1, 31):
            ts = now - timedelta(days=day, minutes=minute)
            db.add(
                Measurement(
                    ts=ts,
                    metric="heart_rate_bpm",
                    source="synthetic",
                    local_date=ts.date(),
                    value=60,
                    unit="bpm",
                )
            )
    for minute in range(31):
        ts = now - timedelta(minutes=60 - minute)
        for metric, value, unit in [("heart_rate_bpm", 100, "bpm"), ("stress_score", 90, "score")]:
            db.add(
                Measurement(
                    ts=ts,
                    metric=metric,
                    source="synthetic",
                    local_date=ts.date(),
                    value=value,
                    unit=unit,
                )
            )
    db.flush()


def test_low_stress_samples_break_elevated_runs(db):
    from sqlalchemy import update

    from garmin_ai.models import Measurement

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    seed_context_measurements(db, now)
    for minute in range(31):
        if minute % 4:
            db.execute(
                update(Measurement)
                .where(
                    Measurement.metric == "stress_score",
                    Measurement.ts == now - timedelta(minutes=60 - minute),
                )
                .values(value=10)
            )
    generate_questions(db, Settings(timezone="UTC"), now)
    assert db.scalar(select(func.count()).select_from(PendingQuestion)) == 0


@pytest.mark.parametrize("change", ["stress", "hr", "count", "baseline", "none"])
def test_context_delivery_rechecks_replaced_measurements(db, change):
    from sqlalchemy import delete, update

    from garmin_ai.models import Measurement

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    settings = Settings(timezone="UTC", proactive_enabled=True)
    seed_context_measurements(db, now)
    generate_questions(db, settings, now)
    q = db.scalar(select(PendingQuestion))
    assert q is not None
    if change == "stress":
        db.execute(update(Measurement).where(Measurement.metric == "stress_score").values(value=10))
    elif change == "hr":
        db.execute(
            update(Measurement)
            .where(
                Measurement.metric == "heart_rate_bpm", Measurement.ts > now - timedelta(hours=2)
            )
            .values(value=40)
        )
    elif change == "count":
        db.execute(
            delete(Measurement).where(
                Measurement.metric == "heart_rate_bpm", Measurement.ts > now - timedelta(minutes=59)
            )
        )
    elif change == "baseline":
        db.execute(delete(Measurement).where(Measurement.ts < now - timedelta(days=1)))
    result = select_question(db, settings, now + timedelta(minutes=30))
    assert (result is not None) == (change == "none")
    assert q.status == ("sending" if change == "none" else "cancelled")


def test_baseline_requires_seven_configured_local_days(db):
    from zoneinfo import ZoneInfo

    from garmin_ai.models import Measurement
    from garmin_ai.proactive import personal_hr_threshold

    zone = ZoneInfo("Europe/Bratislava")
    for day in range(1, 7):
        for minute in range(35):
            ts = datetime(2026, 9, day, tzinfo=zone) + timedelta(minutes=40 * minute)
            db.add(
                Measurement(
                    ts=ts,
                    metric="heart_rate_bpm",
                    source="synthetic",
                    local_date=ts.date(),
                    value=60,
                    unit="bpm",
                )
            )
    db.flush()
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    assert personal_hr_threshold(db, "UTC", now) == 60
    assert personal_hr_threshold(db, "Europe/Bratislava", now) is None


def test_future_migraine_question_cancelled_before_delivery(db):
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    episode = create_event(
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
    )
    settings = Settings(proactive_enabled=True)
    generate_questions(db, settings, now)
    episode.start = now + timedelta(hours=2)
    db.flush()
    assert select_question(db, settings, now) is None
    assert db.scalar(select(PendingQuestion)).status == "cancelled"


@pytest.mark.parametrize("kind", ["medication", "severity"])
def test_undo_followup_fact_clears_acknowledgement(db, kind):
    from garmin_ai.agent import Interpretation, apply_command, context_for
    from garmin_ai.events import undo_last
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    episode = create_event(
        db,
        EventInput(start=now - timedelta(hours=3), payload={"type": "migraine", "severity": 5}),
        actor="owner",
    )
    add_question(db, "migraine", "test", {}, 0.9, "undo-ack", now, event_id=episode.id)
    q = db.scalar(select(PendingQuestion))
    q.status, q.sent_at = "sent", now
    if kind == "medication":
        proposed = EventInput(
            start=now,
            payload={
                "type": "medication",
                "name": "synthetic",
                "dose": 1,
                "unit": "mg",
                "reason_event_id": episode.id,
            },
        )
        command = Interpretation(
            intent="log", target_question_id=q.id, events=[proposed], confidence=1
        )
    else:
        proposed = EventInput(start=episode.start, payload={"type": "migraine", "severity": 7})
        command = Interpretation(
            intent="update",
            target_event_id=episode.id,
            target_question_id=q.id,
            events=[proposed],
            changed_fields=["payload.severity"],
            confidence=1,
        )
    apply_command(db, command, text="synthetic fact", update_id=66, actor="owner", now=now)
    assert q.status == "acknowledged" and q.evidence["answer_text"] == "synthetic fact"
    undo_last(db, actor="owner")
    reconcile_answers(db, now)
    assert q.status == "sent" and q.sent_at == now
    assert not {"answer_text", "answered_at", "acknowledged_events"} & q.evidence.keys()
    assert context_for(db, now)["recent_questions"][0]["evidence"].get("answer_text") is None


@pytest.mark.parametrize("budget", [0, 5])
def test_resume_reports_configured_question_budget(db, db_engine, budget):
    from garmin_ai.telegram import process_message, save_update

    save_update(
        db,
        {
            "update_id": 77,
            "message": {
                "message_id": 77,
                "date": 1788782400,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/resume",
            },
        },
        42,
    )
    db.commit()
    response = process_message(
        db_engine, None, Settings(telegram_user_id=42, question_budget=budget), 77
    )
    assert f"Лимит в день: {budget}" in response


@pytest.mark.parametrize("source", ["activity", "timeline"])
@pytest.mark.parametrize("sent", [False, True])
def test_context_question_recovers_after_explanation_moves(db, source, sent):
    from garmin_ai.models import Activity, TimelineInterval
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    settings = Settings(timezone="UTC", proactive_enabled=True)
    seed_context_measurements(db, now)
    generate_questions(db, settings, now)
    q = db.scalar(select(PendingQuestion))
    if sent:
        q.status, q.sent_at = "sent", now
    left, right = (
        datetime.fromisoformat(q.evidence["start"]),
        datetime.fromisoformat(q.evidence["end"]),
    )
    if source == "activity":
        explanation = Activity(id=998, start=left, end=right, kind="running", timezone="UTC")
    else:
        explanation = TimelineInterval(
            id="corrected-explanation",
            start=left,
            end=right,
            label="workout",
            evidence={},
            confidence=1,
            confirmed=True,
            source="synthetic",
        )
    db.add(explanation)
    db.flush()
    reconcile_answers(db, now)
    assert q.status == "cancelled"
    explanation.start = now - timedelta(days=3)
    explanation.end = now - timedelta(days=3) + timedelta(hours=1)
    db.flush()
    reconcile_answers(db, now)
    assert q.status == ("sent" if sent else "pending")
    assert q.sent_at == (now if sent else None)
    selected = select_question(db, settings, now)
    assert (selected is not None) is not sent
    assert db.scalar(select(func.count()).select_from(PendingQuestion)) == 1


def test_acknowledgement_without_question_id_clarifies(db, db_engine):
    from garmin_ai.agent import Interpretation
    from garmin_ai.models import TelegramUpdate
    from garmin_ai.telegram import process_message, save_update

    class Provider:
        def structured(self, *args):
            return Interpretation(intent="acknowledge", confidence=1)

    save_update(
        db,
        {
            "update_id": 91,
            "message": {
                "message_id": 91,
                "date": 1788782400,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "ещё продолжается",
            },
        },
        42,
    )
    db.commit()
    response = process_message(db_engine, Provider(), Settings(telegram_user_id=42), 91)
    db.expire_all()
    assert "Уточните" in response
    assert db.get(TelegramUpdate, 91).status == "processed"
    assert db.get(AppState, "conversation:pending") is not None


@pytest.mark.parametrize("age,eligible", [(1.9, False), (2, True), (48, True), (49, False)])
@pytest.mark.parametrize("reconcile", [False, True])
def test_migraine_followup_rechecks_age_window(db, age, eligible, reconcile):
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    settings = Settings(proactive_enabled=True)
    episode = create_event(
        db, EventInput(start=now - timedelta(hours=3), payload={"type": "migraine"}), actor="owner"
    )
    generate_questions(db, settings, now)
    q = db.scalar(select(PendingQuestion))
    episode.start = now - timedelta(hours=age)
    db.flush()
    if reconcile:
        reconcile_answers(db, now)
    else:
        select_question(db, settings, now)
    assert (q.status != "cancelled") == eligible


@pytest.mark.parametrize("correction", [None, "stress_score", "heart_rate_bpm"])
def test_answer_undo_revalidates_context_physiology(db, correction):
    from sqlalchemy import update

    from garmin_ai.agent import context_for
    from garmin_ai.events import delete_event
    from garmin_ai.models import Measurement
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    seed_context_measurements(db, now)
    generate_questions(db, Settings(timezone="UTC", proactive_enabled=True), now)
    q = db.scalar(select(PendingQuestion))
    q.status, q.sent_at = "sent", now
    left = datetime.fromisoformat(q.evidence["start"])
    answer = create_event(
        db,
        EventInput(
            start=left,
            timezone="UTC",
            payload={"type": "note", "description": "synthetic explanation"},
        ),
        actor="owner",
    )
    reconcile_answers(db, now)
    assert q.status == "answered"
    if correction:
        db.execute(
            update(Measurement)
            .where(Measurement.metric == correction, Measurement.ts >= left)
            .values(value=30)
        )
    delete_event(db, answer.id, revision=answer.revision, actor="owner")
    reconcile_answers(db, now)
    assert q.status == ("cancelled" if correction else "sent")
    assert q.sent_at == now
    assert bool(context_for(db, now)["recent_questions"]) is (correction is None)


@pytest.mark.parametrize("sent", [False, True])
def test_corrected_context_interval_can_replace_unsent_cancelled_candidate(db, sent):
    from sqlalchemy import delete

    from garmin_ai.models import Measurement
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    settings = Settings(timezone="UTC", proactive_enabled=True)
    seed_context_measurements(db, now)
    generate_questions(db, settings, now)
    previous = db.scalar(select(PendingQuestion))
    if sent:
        previous.status, previous.sent_at = "sent", now
    right = datetime.fromisoformat(previous.evidence["end"])
    db.execute(
        delete(Measurement).where(
            Measurement.metric == "stress_score", Measurement.ts == right - timedelta(minutes=2)
        )
    )
    # Delivery rejects the old endpoints; a new generation should retain the corrected run.
    previous.status = "cancelled"
    reconcile_answers(db, now)
    generate_questions(db, settings, now + timedelta(minutes=30))
    questions = db.scalars(select(PendingQuestion)).all()
    assert len(questions) == 1
    assert previous.status == ("cancelled" if sent else "pending")
    if not sent:
        assert datetime.fromisoformat(previous.evidence["end"]) < right


def test_old_cancelled_migraine_can_be_reconsidered_after_date_correction(db):
    from garmin_ai.proactive import reconcile_answers

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    event = create_event(
        db,
        EventInput(start=now - timedelta(hours=3), timezone="UTC", payload={"type": "migraine"}),
        actor="owner",
    )
    add_question(
        db,
        "migraine",
        "synthetic",
        {},
        0.9,
        f"migraine:{event.id}",
        now - timedelta(days=20),
        event_id=event.id,
    )
    q = db.scalar(select(PendingQuestion))
    q.status = "cancelled"
    reconcile_answers(db, now)
    assert q.status == "pending" and q.expires_at > now
    assert select_question(db, Settings(timezone="UTC", proactive_enabled=True), now).id == q.id


def test_future_migraine_end_remains_open_for_followups(db):
    from garmin_ai.events import update_event
    from garmin_ai.proactive import reconcile_answers

    now = datetime.now(UTC)
    event = create_event(
        db,
        EventInput(start=now - timedelta(hours=3), timezone="UTC", payload={"type": "migraine"}),
        actor="owner",
    )
    generate_questions(db, Settings(timezone="UTC"), now)
    q = db.scalar(select(PendingQuestion))
    updated = EventInput(
        start=event.start,
        end=now + timedelta(hours=2),
        timezone="UTC",
        payload={"type": "migraine"},
    )
    update_event(db, event.id, updated, revision=event.revision, actor="owner")
    reconcile_answers(db, now)
    assert q.status == "pending"
    settings = Settings(
        timezone="UTC", proactive_enabled=True, quiet_start_hour=0, quiet_end_hour=0
    )
    assert select_question(db, settings, now).id == q.id
    q.status = "sent"
    reconcile_answers(db, now + timedelta(hours=3))
    assert q.status == "answered"


@pytest.mark.parametrize("include_seventh", [False, True])
def test_caffeine_habit_counts_only_fourteen_local_calendar_days(db, include_seventh):
    now = datetime(2026, 9, 15, 16, tzinfo=UTC)
    days = [14, 13, 12, 11, 10, 9, 8] + ([7] if include_seventh else [])
    for day in days:
        create_event(
            db,
            EventInput(
                start=(now - timedelta(days=day)).replace(hour=17),
                timezone="UTC",
                payload={"type": "caffeine", "beverage": "coffee"},
            ),
            actor="owner",
        )
    generate_questions(db, Settings(timezone="UTC"), now)
    assert (
        bool(db.scalar(select(PendingQuestion).where(PendingQuestion.kind == "caffeine")))
        is include_seventh
    )


@pytest.mark.parametrize("message", ["synthetic diary", "/undo", "/cancel"])
def test_insights_wait_for_all_pending_owner_messages(db, message):
    from garmin_ai.proactive import can_notify
    from garmin_ai.telegram import save_update

    save_update(
        db,
        {
            "update_id": 9001,
            "message": {
                "message_id": 9001,
                "date": 1788883200,
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": message,
            },
        },
        42,
    )
    assert not can_notify(
        db, Settings(timezone="UTC", proactive_enabled=True), datetime(2026, 9, 8, 12, tzinfo=UTC)
    )


@pytest.mark.parametrize("transport", ["poll", "webhook", "webhook_later"])
def test_notification_jobs_wait_for_upstream_pause_backlog(
    db, db_engine, tmp_path, monkeypatch, transport
):
    import asyncio
    from types import SimpleNamespace

    from garmin_ai import runtime
    from garmin_ai.db import transaction
    from garmin_ai.jobs import enqueue
    from garmin_ai.models import Job, TelegramUpdate
    from garmin_ai.telegram import save_update

    now = datetime.now(UTC)
    create_event(
        db,
        EventInput(start=now - timedelta(hours=3), timezone="UTC", payload={"type": "migraine"}),
        actor="owner",
    )
    if transport != "webhook_later":
        for kind in ("agent_proactive", "agent_insights"):
            enqueue(db, kind, {}, "synthetic:" + kind, now)
    db.commit()
    sent = []
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backup",
        backup_key="",
        timezone="UTC",
        telegram_bot_token="synthetic",
        telegram_user_id=42,
        telegram_webhook_secret="s" * 32,
        proactive_enabled=True,
        quiet_start_hour=0,
        quiet_end_hour=0,
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)
    monkeypatch.setattr(runtime, "GeminiProvider", lambda _: SimpleNamespace(close=lambda: None))

    async def scenario():
        release = asyncio.Event()
        started = asyncio.Event()
        callbacks = []
        pause = {
            "update_id": 9002,
            "message": {
                "message_id": 9002,
                "date": int(now.timestamp()),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/pause",
            },
        }

        class Bot:
            count = 0
            delivered = False

            def __init__(self, *args):
                pass

            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            async def set_webhook(self, **kwargs):
                pass

            async def get_webhook_info(self):
                self.count += 1
                if transport == "poll":
                    return SimpleNamespace(url="")
                if self.count == 1:
                    return SimpleNamespace(url="https://synthetic.invalid", pending_update_count=1)
                if transport == "webhook_later" and self.count == 2:
                    return SimpleNamespace(url="https://synthetic.invalid", pending_update_count=0)
                if transport == "webhook_later" and self.count == 3:
                    started.set()
                    return SimpleNamespace(url="https://synthetic.invalid", pending_update_count=1)
                started.set()
                await release.wait()
                with transaction(db_engine) as session:
                    save_update(session, pause, 42)
                return SimpleNamespace(url="https://synthetic.invalid", pending_update_count=0)

            async def get_updates(self, **kwargs):
                started.set()
                await release.wait()
                if not self.delivered:
                    self.delivered = True
                    return [SimpleNamespace(update_id=9002, to_dict=lambda: pause)]
                await asyncio.sleep(0.01)
                return []

            async def send_message(self, **kwargs):
                sent.append(kwargs["text"])
                return SimpleNamespace(message_id=1)

        monkeypatch.setattr(runtime, "Bot", Bot)
        monkeypatch.setattr(
            asyncio.get_running_loop(), "add_signal_handler", lambda s, cb: callbacks.append(cb)
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            await asyncio.wait_for(started.wait(), 3)
            if transport == "webhook_later":
                sent.clear()  # Notifications before the later backlog were legitimate.
                with transaction(db_engine) as session:
                    for kind in ("agent_proactive", "agent_insights"):
                        enqueue(session, kind, {}, "synthetic:" + kind, now)
            await asyncio.sleep(0.1)
            db.expire_all()
            for job in db.scalars(select(Job).where(Job.dedup_key.like("synthetic:%"))):
                assert job.status == "pending" and job.attempts == 0
            db.rollback()
            release.set()
            for _ in range(150):
                await asyncio.sleep(0.02)
                db.expire_all()
                row = db.get(TelegramUpdate, 9002)
                if row and row.status == "processed":
                    break
            assert row.status == "processed"
            await asyncio.sleep(1.1)
            assert sent and all("Вопросы отключены" in text for text in sent)
        finally:
            release.set()
            callbacks[0]()
            await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


def test_migraine_edit_waits_until_reserved_question_delivery_finishes(
    db, db_engine, tmp_path, monkeypatch
):
    import asyncio
    import threading
    from types import SimpleNamespace

    from garmin_ai import runtime
    from garmin_ai.db import transaction
    from garmin_ai.events import update_event
    from garmin_ai.jobs import enqueue
    from garmin_ai.models import Event

    now = datetime.now(UTC)
    episode = create_event(
        db,
        EventInput(start=now - timedelta(hours=3), timezone="UTC", payload={"type": "migraine"}),
        actor="owner",
    )
    identity = episode.id
    enqueue(db, "agent_proactive", {}, "synthetic:delivery", now)
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="synthetic",
        telegram_user_id=42,
        timezone="UTC",
        proactive_enabled=True,
        quiet_start_hour=0,
        quiet_end_hour=0,
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)
    monkeypatch.setattr(runtime, "GeminiProvider", lambda _: SimpleNamespace(close=lambda: None))
    editing = threading.Event()

    def close_episode():
        with transaction(db_engine) as session:
            row = session.get(Event, identity)
            editing.set()
            update_event(
                session,
                identity,
                EventInput(
                    start=row.start,
                    end=datetime.now(UTC),
                    timezone="UTC",
                    payload={"type": "migraine"},
                ),
                revision=row.revision,
                actor="api",
            )

    async def scenario():
        sending = asyncio.Event()
        release = asyncio.Event()
        callbacks = []

        class Bot:
            def __init__(self, *args):
                pass

            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            async def get_webhook_info(self):
                return SimpleNamespace(url="")

            async def get_updates(self, **kwargs):
                await asyncio.sleep(0.01)
                return []

            async def send_message(self, **kwargs):
                sending.set()
                await release.wait()
                return SimpleNamespace(message_id=1)

        monkeypatch.setattr(runtime, "Bot", Bot)
        monkeypatch.setattr(
            asyncio.get_running_loop(), "add_signal_handler", lambda s, cb: callbacks.append(cb)
        )
        task = asyncio.create_task(runtime.run(settings))
        edit = None
        try:
            await asyncio.wait_for(sending.wait(), 3)
            edit = asyncio.create_task(asyncio.to_thread(close_episode))
            assert await asyncio.to_thread(editing.wait, 2)
            await asyncio.sleep(0.1)
            assert not edit.done()
            release.set()
            await asyncio.wait_for(edit, 3)
            db.expire_all()
            q = db.scalar(select(PendingQuestion).where(PendingQuestion.event_id == identity))
            assert q.status == "answered"
        finally:
            release.set()
            if edit:
                await edit
            callbacks[0]()
            await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


@pytest.mark.parametrize("feed", ["activities", "heart_rate", "stress"])
def test_failed_sync_suppresses_context_until_replacement_succeeds(db, feed):
    from garmin_ai.jobs import claim, enqueue
    from garmin_ai.models import Job

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    kind = "garmin_activities" if feed == "activities" else "garmin_endpoint"
    payload = {"offset": 0} if feed == "activities" else {"endpoint": feed, "key": "2026-09-08"}
    dependency = enqueue(db, kind, payload, "synthetic-failed", now - timedelta(minutes=1))
    failed = db.get(Job, dependency)
    failed.status, failed.attempts = "failed", 8
    seed_context_measurements(db, now)
    # Diary follow-ups do not depend on Garmin activity or physiological completeness.
    create_event(
        db,
        EventInput(start=now - timedelta(hours=3), timezone="UTC", payload={"type": "migraine"}),
        actor="owner",
    )
    identity = enqueue(db, "agent_proactive", {}, "synthetic-proactive", now)
    claimed = claim(db, now=now, kinds=["agent_proactive"])
    assert claimed.id == identity and claimed.payload["context_sync_failures"] == [str(dependency)]
    settings = Settings(timezone="UTC", proactive_enabled=True)
    generate_questions(
        db, settings, now, allow_context=not claimed.payload["context_sync_failures"]
    )
    assert {q.kind for q in db.scalars(select(PendingQuestion))} == {"migraine"}
    add_question(
        db,
        "context",
        "synthetic",
        {
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now - timedelta(minutes=28)).isoformat(),
        },
        1,
        "synthetic-context",
        now,
    )
    assert select_question(db, settings, now, allow_context=False).kind == "migraine"
    key = "freshness:activities:page:0" if feed == "activities" else f"freshness:{feed}:2026-09-08"
    db.add(AppState(key=key, value={"success_at": now.isoformat()}))
    next_id = enqueue(db, "agent_proactive", {}, "synthetic-recovered", now)
    db.flush()
    recovered = claim(db, now=now, kinds=["agent_proactive"])
    assert recovered.id == next_id and recovered.payload["context_sync_failures"] == []


def test_future_ended_episode_counts_in_ambiguous_close_context(db):
    import json

    from garmin_ai.agent import Interpretation, interpret

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    for end in (None, now + timedelta(hours=2)):
        create_event(
            db,
            EventInput(
                start=now - timedelta(hours=3),
                end=end,
                timezone="UTC",
                payload={"type": "migraine"},
            ),
            actor="owner",
        )

    class Provider:
        def structured(self, instruction, prompt, schema):
            assert json.loads(prompt)["context"]["open_migraine_count"] == 2
            return Interpretation(intent="clarify", confidence=1, clarification="Уточните эпизод")

    assert (
        interpret(db, Provider(), "Закончилась сейчас", Settings(timezone="UTC"), now).intent
        == "clarify"
    )


def test_context_generation_recovers_inside_same_slot(db):
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    settings = Settings(timezone="UTC")
    seed_context_measurements(db, now)
    generate_questions(db, settings, now, allow_context=False)
    assert db.scalar(select(PendingQuestion)) is None
    generate_questions(db, settings, now + timedelta(seconds=20), allow_context=True)
    assert db.scalar(select(PendingQuestion)).kind == "context"
    generate_questions(db, settings, now + timedelta(seconds=40), allow_context=True)
    assert db.scalar(select(func.count()).select_from(PendingQuestion)) == 1


def test_acknowledgement_waits_for_concurrent_episode_edit(db, db_engine):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from garmin_ai.agent import Interpretation, apply_command
    from garmin_ai.db import transaction
    from garmin_ai.events import update_event
    from garmin_ai.models import Event

    now = datetime.now(UTC)
    episode = create_event(
        db,
        EventInput(start=now - timedelta(hours=3), timezone="UTC", payload={"type": "migraine"}),
        actor="owner",
    )
    identity = episode.id
    add_question(db, "migraine", "synthetic", {}, 0.9, "synthetic-ack-race", now, event_id=identity)
    question = db.scalar(select(PendingQuestion))
    question.status = "sent"
    question.sent_at = now
    question_id = question.id
    db.commit()
    started = threading.Event()

    def acknowledge():
        with transaction(db_engine) as session:
            session.get(PendingQuestion, question_id)
            started.set()
            return apply_command(
                session,
                Interpretation(intent="acknowledge", confidence=1, target_question_id=question_id),
                text="Ещё продолжается",
                update_id=9876,
                actor="owner",
                now=now,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(db_engine) as session:
            row = session.get(Event, identity)
            update_event(
                session,
                identity,
                EventInput(start=row.start, end=now, timezone="UTC", payload={"type": "migraine"}),
                revision=row.revision,
                actor="api",
            )
            future = pool.submit(acknowledge)
            assert started.wait(2)
            time.sleep(0.1)
            assert not future.done()
        response = future.result(timeout=3)
    assert "изменилась" in response
    db.expire_all()
    assert db.get(PendingQuestion, question_id).status == "answered"


def test_runtime_retries_suppressed_context_after_feed_recovery(
    db, db_engine, tmp_path, monkeypatch
):
    import asyncio

    from garmin_ai import runtime
    from garmin_ai.jobs import enqueue
    from garmin_ai.models import Job

    now = datetime.now(UTC)
    seed_context_measurements(db, now)
    dependency = enqueue(
        db, "garmin_activities", {"offset": 0}, "synthetic-failure", now - timedelta(minutes=1)
    )
    row = db.get(Job, dependency)
    row.status, row.attempts = "failed", 8
    identity = enqueue(db, "agent_proactive", {}, "synthetic-retry-context", now)
    db.commit()
    settings = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
        backup_key="",
        telegram_bot_token="",
        llm_enabled=False,
        timezone="UTC",
    )
    monkeypatch.setattr(runtime, "make_engine", lambda _: db_engine)

    async def scenario():
        callbacks = []
        monkeypatch.setattr(
            asyncio.get_running_loop(), "add_signal_handler", lambda s, cb: callbacks.append(cb)
        )
        task = asyncio.create_task(runtime.run(settings))
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                db.expire_all()
                job = db.get(Job, identity)
                if job.last_error == "DiaryDeferred":
                    break
            assert (
                job.status == "pending" and job.attempts == 0 and job.last_error == "DiaryDeferred"
            )
            assert db.scalar(select(PendingQuestion)) is None
            db.add(
                AppState(
                    key="freshness:activities:page:0",
                    value={"success_at": datetime.now(UTC).isoformat()},
                )
            )
            job.run_at = datetime.now(UTC)
            db.commit()
            for _ in range(100):
                await asyncio.sleep(0.02)
                db.expire_all()
                job = db.get(Job, identity)
                if job.status == "done":
                    break
            assert job.status == "done"
            assert db.scalar(select(PendingQuestion)).kind == "context"
        finally:
            db.rollback()
            callbacks[0]()
            await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


@pytest.mark.parametrize("expired", [False, True])
def test_recovered_feed_compares_actual_terminal_failure_time(db, expired):
    from garmin_ai.jobs import claim, enqueue, failed_context_sync, finish

    now = datetime.now(UTC)
    identity = enqueue(db, "garmin_activities", {"offset": 0}, "terminal-feed", now)
    job = claim(db, now=now, kinds=["garmin_activities"])
    job.attempts = 8
    if expired:
        job.lease_until = now - timedelta(seconds=1)
        db.flush()
        claim(db, now=now, kinds=["garmin_activities"])
    else:
        db.flush()
        finish(db, identity, job.lease_token, error_type="SyntheticError")
    db.flush()
    db.refresh(job)
    assert job.status == "failed" and job.completed_at is not None
    # Even a retained legacy future schedule cannot postpone recovery.
    job.run_at = now + timedelta(hours=1)
    assert failed_context_sync(db, now) == [str(identity)]
    db.add(
        AppState(
            key="freshness:activities:page:0",
            value={"success_at": (job.completed_at + timedelta(seconds=1)).isoformat()},
        )
    )
    db.flush()
    assert failed_context_sync(db, now + timedelta(seconds=2)) == []
    assert failed_context_sync(db, now + timedelta(hours=4)) == []


def test_caffeine_absence_requires_bounded_interval():
    from pydantic import ValidationError

    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="Caffeine absence requires an end"):
        EventInput(start=now, payload={"type": "caffeine_absence", "description": "none"})
    assert EventInput(start=now, payload={"type": "migraine"}).end is None
    assert EventInput(
        start=now,
        end=now + timedelta(hours=1),
        payload={"type": "caffeine_absence", "description": "none"},
    ).end


def test_proactive_recovery_deadline_survives_retries(db):
    from garmin_ai.jobs import claim, enqueue, finish

    now = datetime.now(UTC)
    enqueue(db, "agent_proactive", {}, "proactive:synthetic", now)
    job = claim(db, now=now, kinds=["agent_proactive"])
    deadline = job.payload["context_expires_at"]
    assert datetime.fromisoformat(deadline) == now + timedelta(minutes=30)
    finish(db, job.id, job.lease_token, error_type="DiaryDeferred")
    db.flush()
    again = claim(db, now=now + timedelta(minutes=31), kinds=["agent_proactive"])
    assert again.payload["context_expires_at"] == deadline
    assert datetime.fromisoformat(deadline) < now + timedelta(minutes=31)


@pytest.mark.parametrize("operation", ["ingest", "activity"])
def test_garmin_writes_wait_for_context_delivery_reservation(db, db_engine, tmp_path, operation):
    import concurrent.futures
    import time

    from sqlalchemy import text

    from garmin_ai.archive import LocalArchive
    from garmin_ai.db import transaction
    from garmin_ai.ingest import ingest
    from garmin_ai.normalize import normalize_activity

    def write():
        with transaction(db_engine) as session:
            if operation == "ingest":
                ingest(session, LocalArchive(tmp_path / "raw"), "stress", "2026-09-08", {}, "UTC")
            else:
                normalize_activity(
                    session,
                    {"activityId": 9, "startTimeGMT": "2026-09-08 12:00:00", "duration": 60},
                    "UTC",
                )

    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("SELECT pg_advisory_lock(72104619)"))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(write)
            try:
                time.sleep(0.1)
                assert not future.done()
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(72104619)"))
            future.result(timeout=3)
