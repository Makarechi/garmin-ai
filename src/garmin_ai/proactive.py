"""Evidence-driven questions with persistent budgets and no automatic repeats."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.analytics import compare_periods
from garmin_ai.events import reactivate_question
from garmin_ai.freshness import covered_seconds
from garmin_ai.models import (
    Activity,
    AppState,
    Event,
    HealthDay,
    Insight,
    Measurement,
    MessageDeliveryReceipt,
    OutboxMessage,
    PendingQuestion,
    TelegramUpdate,
    TimelineInterval,
)
from garmin_ai.normalize import upsert

CONTEXT_KINDS = {
    "context",
    "nap",
    "meal",
    "stressor",
    "travel",
    "mood",
    "note",
    "illness",
    "alcohol",
    "hydration",
}


def enabled(session, settings):
    state = session.get(AppState, "proactive:enabled")
    return state.value["enabled"] if state else settings.proactive_enabled


def add_question(session, kind, text, evidence, priority, key, now, event_id=None, delay=0):
    if kind == "context" and evidence.get("start") and evidence.get("end"):
        session.execute(select(func.pg_advisory_xact_lock(72104621)))
        left, right = (
            datetime.fromisoformat(evidence["start"]),
            datetime.fromisoformat(evidence["end"]),
        )
        for existing in session.scalars(
            select(PendingQuestion).where(
                PendingQuestion.kind == "context",
                PendingQuestion.expires_at > now,
                or_(PendingQuestion.status != "cancelled", PendingQuestion.sent_at.is_not(None)),
            )
        ):
            before = existing.evidence
            if (
                before.get("start")
                and before.get("end")
                and datetime.fromisoformat(before["start"]) < right
                and datetime.fromisoformat(before["end"]) > left
            ):
                return
        reusable = session.scalar(
            select(PendingQuestion).where(
                PendingQuestion.dedup_key == key,
                PendingQuestion.kind == "context",
                PendingQuestion.status == "cancelled",
                PendingQuestion.sent_at.is_(None),
            )
        )
        if reusable and reusable.evidence.get("cancel_reason") == "owner_pause":
            return
        if reusable:
            reusable.text, reusable.evidence, reusable.priority = text, evidence, priority
            reusable.earliest_send_at = now + timedelta(seconds=delay)
            reusable.expires_at = now + timedelta(days=2)
            reusable.status = "pending"
            return
    session.execute(
        insert(PendingQuestion)
        .values(
            kind=kind,
            text=text,
            evidence=evidence,
            priority=priority,
            dedup_key=key,
            event_id=event_id,
            earliest_send_at=now + timedelta(seconds=delay),
            expires_at=now + timedelta(days=2),
        )
        .on_conflict_do_nothing(index_elements=[PendingQuestion.dedup_key])
    )


def context_coverage(session, left, right):
    """Union of actual bounded context; points and calendar plans cover no duration."""
    if right <= left:
        raise ValueError("Context interval must be positive")
    intervals = list(
        session.execute(
            select(Activity.start, Activity.end).where(
                Activity.start < right,
                Activity.end > left,
            )
        ).all()
    )
    intervals.extend(
        session.execute(
            select(TimelineInterval.start, TimelineInterval.end).where(
                TimelineInterval.start < right,
                TimelineInterval.end > left,
                TimelineInterval.confirmed.is_(True),
                TimelineInterval.source.not_in(["calendar", "external_calendar"]),
            )
        ).all()
    )
    intervals.extend(
        session.execute(
            select(Event.start, Event.end).where(
                Event.deleted.is_(False),
                Event.kind.in_(CONTEXT_KINDS),
                Event.status == "confirmed",
                Event.source != "inferred",
                Event.start < right,
                Event.end > left,
            )
        ).all()
    )
    cursor = left
    gaps = []
    for start, end in sorted(intervals):
        start, end = max(left, start), min(right, end)
        if end <= start:
            continue
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < right:
        gaps.append((cursor, right))
    missing = sum((end - start).total_seconds() for start, end in gaps)
    duration = (right - left).total_seconds()
    return {
        "covered_seconds": duration - missing,
        "uncovered_seconds": missing,
        "coverage_ratio": (duration - missing) / duration,
        "uncovered_intervals": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in gaps],
    }


def context_explained(session, left, right):
    return context_coverage(session, left, right)["uncovered_seconds"] == 0


def personal_hr_threshold(session, timezone, now):
    hr = session.execute(
        select(Measurement.ts, Measurement.value).where(
            Measurement.metric == "heart_rate_bpm",
            Measurement.quality == "observed",
            Measurement.source == "garmin_connect",
            Measurement.ts >= now - timedelta(days=14),
            Measurement.ts < now - timedelta(days=1),
        )
    ).all()
    zone = ZoneInfo(timezone)
    if len(hr) < 200 or len({r.ts.astimezone(zone).date() for r in hr}) < 7:
        return None
    return float(np.quantile([r.value for r in hr], 0.95))


def elevated_stress_runs(session, left, right):
    points = session.execute(
        select(Measurement.ts, Measurement.value)
        .where(
            Measurement.metric == "stress_score",
            Measurement.quality == "observed",
            Measurement.source == "garmin_connect",
            Measurement.ts >= left,
            Measurement.ts < right,
        )
        .order_by(Measurement.ts)
    ).all()
    runs, current = [], []
    for point in points:
        if point.value < 85 or (current and point.ts - current[-1] > timedelta(minutes=5)):
            if current:
                runs.append(current)
            current = []
        if point.value >= 85:
            current.append(point.ts)
    if current:
        runs.append(current)
    return [run for run in runs if run[-1] - run[0] >= timedelta(minutes=20)]


def context_physiology(session, timezone, now, left, right, *, threshold=None):
    if right > now or right <= left:
        return None
    threshold = (
        threshold if threshold is not None else personal_hr_threshold(session, timezone, now)
    )
    if threshold is None:
        return None
    runs = elevated_stress_runs(session, left, right)
    if not any(run[0] == left and run[-1] + timedelta(minutes=2) == right for run in runs):
        return None
    samples = session.execute(
        select(Measurement.ts, Measurement.value).where(
            Measurement.metric == "heart_rate_bpm",
            Measurement.quality == "observed",
            Measurement.source == "garmin_connect",
            Measurement.ts >= left,
            Measurement.ts < right,
        )
    ).all()
    values = [r.value for r in samples]
    coverage = (
        covered_seconds([r.ts for r in samples], left, right, 300) / (right - left).total_seconds()
    )
    if coverage < 0.8:
        return None
    if len(values) < 5 or float(np.mean(values)) < threshold:
        return None
    return {"baseline_hr_p95": threshold, "hr_samples": len(values), "hr_coverage_ratio": coverage}


def generate_questions(session, settings, now, *, allow_context=True):
    from garmin_ai.accounts import effective_owner_settings

    session.execute(select(func.pg_advisory_xact_lock(72104621)))
    settings = effective_owner_settings(session, settings)
    owner_control = session.get(AppState, "proactive:enabled", populate_existing=True)
    if owner_control is not None and owner_control.value.get("enabled") is False:
        return
    from garmin_ai.scenario_packs import pack_enabled

    slot = int(now.timestamp()) // 1800
    state = session.get(AppState, "proactive:generation")
    if (
        state
        and state.value.get("slot") == slot
        and (state.value.get("context_complete", True) or not allow_context)
    ):
        return
    upsert(
        session,
        AppState,
        dict(key="proactive:generation", value={"slot": slot, "context_complete": allow_context}),
        ["key"],
    )
    if pack_enabled(session, "migraine", "reminders") and pack_enabled(
        session, "migraine", "tracking"
    ):
        for e in session.scalars(
            select(Event).where(
                Event.deleted.is_(False),
                Event.kind == "migraine",
                Event.status == "confirmed",
                or_(Event.end.is_(None), Event.end > now),
                Event.start <= now - timedelta(hours=2),
                Event.start >= now - timedelta(days=2),
            )
        ):
            message = migraine_question_text(session, e, now)
            add_question(
                session,
                "migraine",
                message,
                {"event_id": str(e.id)},
                0.95,
                f"migraine:{e.id}",
                now,
                event_id=e.id,
            )
    local = now.astimezone(ZoneInfo(settings.timezone))
    recent = session.scalars(
        select(Event).where(
            Event.kind == "caffeine",
            Event.deleted.is_(False),
            Event.status == "confirmed",
            Event.start
            >= datetime.combine(
                local.date() - timedelta(days=13), datetime.min.time(), local.tzinfo
            ),
            Event.start <= now,
        )
    ).all()
    days = {e.start.astimezone(ZoneInfo(settings.timezone)).date() for e in recent}
    ignored = session.scalar(
        select(func.count())
        .select_from(PendingQuestion)
        .where(
            PendingQuestion.kind == "caffeine",
            PendingQuestion.status.in_(["sent", "uncertain"]),
            PendingQuestion.sent_at >= now - timedelta(days=7),
        )
    )
    left = datetime.combine(local.date(), datetime.min.time(), ZoneInfo(settings.timezone))
    absent = session.scalar(
        select(Event.id)
        .where(
            Event.kind == "caffeine_absence",
            Event.deleted.is_(False),
            Event.status == "confirmed",
            Event.start <= left,
            Event.end >= now,
        )
        .limit(1)
    )
    if (
        pack_enabled(session, "caffeine", "reminders")
        and pack_enabled(session, "caffeine", "tracking")
        and local.hour >= 15
        and len(days) >= 7
        and local.date() not in days
        and not ignored
        and not absent
    ):
        add_question(
            session,
            "caffeine",
            f"{local:%d.%m.%Y} кофе был? Если да — примерно когда и сколько?",
            {
                "logged_days_last_14": len(days),
                "day": str(local.date()),
                "timezone": settings.timezone,
            },
            0.6,
            f"caffeine:{local.date()}",
            now,
        )
    from garmin_ai.initiative_rules import queue_due_tracker_checkins

    queue_due_tracker_checkins(session, settings, now)
    from garmin_ai.scenario_packs import question_enabled

    if not allow_context or not question_enabled(session, "context", "reminders"):
        return
    threshold = personal_hr_threshold(session, settings.timezone, now)
    if threshold is None:
        return
    for points in elevated_stress_runs(
        session, now - timedelta(hours=3), now - timedelta(minutes=15)
    ):
        left, right = points[0], points[-1] + timedelta(minutes=2)
        evidence = context_physiology(
            session, settings.timezone, now, left, right, threshold=threshold
        )
        if evidence is None or context_explained(session, left, right):
            continue
        a = left.astimezone(ZoneInfo(settings.timezone))
        b = right.astimezone(ZoneInfo(settings.timezone))
        ending = b.strftime("%d.%m.%Y %H:%M" if a.date() != b.date() else "%H:%M")
        add_question(
            session,
            "context",
            f"{a:%d.%m.%Y} с {a:%H:%M} до {ending} часы записали повышенные показатели стресса и пульса. Контекст этого интервала известен не полностью. Помните, чем занимались? Можно ответить «не помню».",
            {
                "start": left.isoformat(),
                "end": right.isoformat(),
                **evidence,
                "timezone": settings.timezone,
                "status": "inferred",
                "context_coverage": context_coverage(session, left, right),
            },
            0.7,
            f"context:{left.isoformat()}",
            now,
        )


def reconcile_answers(session, now):
    from garmin_ai.scenario_packs import question_enabled

    for question in session.scalars(
        select(PendingQuestion).where(
            or_(
                PendingQuestion.expires_at >= now - timedelta(days=7),
                (PendingQuestion.kind == "migraine") & (PendingQuestion.status == "cancelled"),
            ),
            PendingQuestion.status.in_(
                ["pending", "sent", "uncertain", "answered", "acknowledged", "cancelled"]
            ),
        )
    ):
        if (
            question.status == "cancelled"
            and question.evidence.get("cancel_reason") == "owner_pause"
        ):
            continue
        if not question_enabled(session, question.kind, "reminders"):
            question.status = "cancelled"
            continue
        acknowledged = question.evidence.get("acknowledged_events", {})
        if acknowledged and any(
            (event := session.get(Event, UUID(identity), populate_existing=True)) is None
            or event.deleted
            or event.revision != revision
            for identity, revision in acknowledged.items()
        ):
            reactivate_question(question, now)
        answer = None
        if question.kind == "migraine" and question.event_id:
            episode = session.get(Event, question.event_id, populate_existing=True)
            if (
                not episode
                or episode.deleted
                or episode.kind != "migraine"
                or episode.status != "confirmed"
                or episode.start > now
            ):
                question.status = "cancelled"
                continue
            if (episode.end is None or episode.end > now) and not now - timedelta(
                days=2
            ) <= episode.start <= now - timedelta(hours=2):
                question.status = "cancelled"
                continue
            answer = episode if episode.end and episode.end <= now else None
        elif question.kind == "caffeine" and question.evidence.get("day"):
            zone = ZoneInfo(question.evidence.get("timezone", "Europe/Bratislava"))
            left = datetime.fromisoformat(question.evidence["day"]).replace(tzinfo=zone)
            answer = session.scalar(
                select(Event)
                .where(
                    Event.deleted.is_(False),
                    Event.status == "confirmed",
                    or_(
                        (Event.kind == "caffeine")
                        & (Event.start >= left)
                        & (Event.start < left + timedelta(days=1))
                        & (Event.start <= now),
                        (Event.kind == "caffeine_absence")
                        & (Event.start <= left)
                        & (
                            Event.end
                            >= min(
                                now,
                                left + timedelta(days=1),
                                question.sent_at or question.earliest_send_at,
                            )
                        ),
                    ),
                )
                .order_by(Event.start)
                .limit(1)
            )
        elif question.kind == "context" and question.evidence.get("start"):
            left, right = (
                datetime.fromisoformat(question.evidence["start"]),
                datetime.fromisoformat(question.evidence["end"]),
            )
            coverage = context_coverage(session, left, right)
            question.evidence = {**question.evidence, "context_coverage": coverage}
            if (
                question.status in {"acknowledged", "cancelled"}
                and question.evidence.get("answer_kind") == "unknown"
            ):
                question.status = (
                    "cancelled" if coverage["uncovered_seconds"] == 0 else "acknowledged"
                )
                continue
            replies = question.evidence.get("reply_events", {})
            if replies and all(
                (event := session.get(Event, UUID(identity), populate_existing=True)) is not None
                and not event.deleted
                and event.revision == revision
                for identity, revision in replies.items()
            ):
                question.status = "answered"
                continue
            left, right = (
                datetime.fromisoformat(question.evidence["start"]),
                datetime.fromisoformat(question.evidence["end"]),
            )
            answer = session.scalar(
                select(Event)
                .where(
                    Event.deleted.is_(False),
                    Event.status == "confirmed",
                    Event.kind.in_(CONTEXT_KINDS),
                    Event.start <= left,
                    Event.end >= right,
                )
                .order_by(Event.start)
                .limit(1)
            )
            if answer is None and coverage["uncovered_seconds"] == 0:
                question.status = "cancelled"
                continue
            if answer is None and question.status in {"cancelled", "answered"}:
                evidence = context_physiology(
                    session,
                    question.evidence.get(
                        "timezone", session.info.get("timezone", "Europe/Bratislava")
                    ),
                    now,
                    left,
                    right,
                )
                if evidence is None:
                    question.status = "cancelled"
                    continue
                if evidence is not None:
                    reactivate_question(question, now)
                    question.evidence = {**question.evidence, **evidence}
        else:
            continue
        if answer:
            question.status = "answered"
            question.evidence = {**question.evidence, "answer_event_id": str(answer.id)}
        elif question.status == "answered" or (
            question.status == "cancelled" and question.kind == "migraine"
        ):
            # Restore unanswered conversation context without repeating a delivered prompt.
            reactivate_question(question, now)


def reconcile_questions(session):
    reconcile_answers(session, datetime.now(UTC))
    for question in session.scalars(
        select(PendingQuestion).where(PendingQuestion.status == "sending").with_for_update()
    ):
        outbox = session.get(AppState, f"outbox:question:{question.id}:0")
        if outbox is None or outbox.value["status"] == "pending":
            question.status = "pending"
            question.sent_at = None
        else:
            question.status = "sent" if outbox.value["status"] == "sent" else "uncertain"


def release_unsent_question(session, question_id):
    """Retire a selected question if pause committed before the send fence."""
    question = session.get(PendingQuestion, question_id, populate_existing=True)
    if question is None or question.status != "sending":
        return
    control = session.get(AppState, "proactive:enabled", populate_existing=True)
    paused = control is not None and control.value.get("enabled") is False
    question.status = "cancelled" if paused else "pending"
    if paused:
        question.evidence = {**question.evidence, "cancel_reason": "owner_pause"}
    question.sent_at = None


def select_question(session, settings, now, *, allow_context=True):
    from garmin_ai.accounts import effective_owner_settings

    settings = effective_owner_settings(session, settings)
    from garmin_ai.scenario_packs import question_enabled

    session.execute(select(func.pg_advisory_xact_lock(72104621)))
    if not can_notify(session, settings, now):
        return None
    for q in session.scalars(
        select(PendingQuestion)
        .where(
            PendingQuestion.status == "pending",
            PendingQuestion.priority >= 0.6,
            PendingQuestion.earliest_send_at <= now,
            PendingQuestion.expires_at > now,
        )
        .order_by(PendingQuestion.priority.desc())
        .with_for_update(skip_locked=True)
    ):
        if not question_enabled(session, q.kind, "reminders"):
            q.status = "cancelled"
            continue
        if q.kind == "context" and not allow_context:
            continue
        if q.event_id:
            event = session.get(Event, q.event_id, populate_existing=True)
            if (
                not event
                or event.deleted
                or event.kind != "migraine"
                or event.status != "confirmed"
                or event.start > now
            ):
                q.status = "cancelled"
                continue
            if event.end and event.end <= now:
                q.status = "answered"
                continue
            if not now - timedelta(days=2) <= event.start <= now - timedelta(hours=2):
                q.status = "cancelled"
                continue
        if q.kind == "context" and q.evidence.get("start") and q.evidence.get("end"):
            left = datetime.fromisoformat(q.evidence["start"])
            right = datetime.fromisoformat(q.evidence["end"])
            evidence = context_physiology(
                session, q.evidence.get("timezone", settings.timezone), now, left, right
            )
            coverage = context_coverage(session, left, right)
            q.evidence = {**q.evidence, "context_coverage": coverage}
            if evidence is None or coverage["uncovered_seconds"] == 0:
                q.status = "cancelled"
                continue
            q.evidence = {**q.evidence, **evidence}
            zone = ZoneInfo(q.evidence.get("timezone", settings.timezone))
            gaps = coverage["uncovered_intervals"]
            windows = "; ".join(
                f"{datetime.fromisoformat(gap['start']).astimezone(zone):%d.%m %H:%M}–{datetime.fromisoformat(gap['end']).astimezone(zone):%d.%m %H:%M}"
                for gap in gaps[:3]
            )
            q.text = (
                "Часы записали повышенные показатели стресса и пульса. "
                f"Неизвестный контекст: {coverage['uncovered_seconds'] / 60:g} мин; {windows}"
                + ("; есть другие промежутки" if len(gaps) > 3 else "")
                + ". Помните, чем занимались? Можно ответить «не помню»."
            )
        recent = session.scalar(
            select(PendingQuestion.id)
            .where(
                PendingQuestion.kind == q.kind, PendingQuestion.sent_at >= now - timedelta(hours=24)
            )
            .limit(1)
        )
        if recent:
            continue
        if q.kind == "migraine":
            q.text = migraine_question_text(session, event, now)
        q.status = "sending"
        q.sent_at = now
        q.expires_at = now + timedelta(days=2)
        q.attempts += 1
        return q
    return None


def notification_count(session, settings, now, *, exclude_insight_key=None, exclude_outbox_id=None):
    if hasattr(settings, "locale") and hasattr(settings, "units"):
        from garmin_ai.accounts import effective_owner_settings

        settings = effective_owner_settings(session, settings)
    local = now.astimezone(ZoneInfo(settings.timezone))
    day_start = datetime.combine(local.date(), datetime.min.time(), local.tzinfo)
    questions = session.scalar(
        select(func.count())
        .select_from(PendingQuestion)
        .where(PendingQuestion.sent_at >= day_start, PendingQuestion.sent_at <= now)
    )
    insights = sum(
        1
        for row in session.scalars(
            select(AppState)
            .where(AppState.key.startswith("insight:last:"))
            .execution_options(populate_existing=True)
        )
        if row.key != exclude_insight_key
        and day_start <= datetime.fromisoformat(row.value["at"]) <= now
    )
    next_day = day_start + timedelta(days=1)
    initiative_query = (
        select(func.count())
        .select_from(OutboxMessage)
        .where(
            OutboxMessage.intent["initiative"].as_boolean().is_(True),
            OutboxMessage.state.not_in(["cancelled", "failed", "expired"]),
            or_(
                (
                    or_(
                        (OutboxMessage.created_at >= day_start)
                        & (OutboxMessage.created_at < next_day),
                        OutboxMessage.dedup_key.endswith(":" + local.date().isoformat()),
                    )
                    & or_(
                        OutboxMessage.next_attempt_at.is_(None),
                        OutboxMessage.next_attempt_at < next_day,
                    )
                ),
                (OutboxMessage.next_attempt_at >= day_start)
                & (OutboxMessage.next_attempt_at < next_day),
                select(func.min(MessageDeliveryReceipt.observed_at))
                .where(
                    MessageDeliveryReceipt.outbox_message_id == OutboxMessage.id,
                    MessageDeliveryReceipt.state.in_(
                        ["provider_accepted", "delivered", "read", "uncertain"]
                    ),
                )
                .correlate(OutboxMessage)
                .scalar_subquery()
                .between(day_start, now),
            ),
        )
    )
    if exclude_outbox_id is not None:
        initiative_query = initiative_query.where(OutboxMessage.id != exclude_outbox_id)
    initiatives = session.scalar(initiative_query)
    return questions + insights + initiatives


def pending_insight_notices(session, now):
    from garmin_ai.scenario_packs import insight_enabled, insight_filter

    reserved = (
        select(AppState.key)
        .where(
            AppState.key.startswith("insight:last:"),
            AppState.value["reservation"].astext == cast(Insight.id, String),
        )
        .exists()
    )
    rows = session.scalars(
        select(Insight)
        .where(
            Insight.status == "accepted",
            or_(Insight.generated_at >= now - timedelta(days=1), reserved),
            insight_filter(session),
        )
        .order_by(reserved.desc(), Insight.generated_at.desc(), Insight.id)
        .limit(3)
    ).all()
    return [row for row in rows if insight_enabled(session, row)]


def reserve_insight_notice(session, settings, now, insight):
    from garmin_ai.accounts import effective_owner_settings

    settings = effective_owner_settings(session, settings)
    from garmin_ai.scenario_packs import insight_enabled

    if not insight_enabled(session, insight):
        return False
    session.execute(select(func.pg_advisory_xact_lock(72104621)))
    key = f"insight:last:{insight.dedup_key.split(':')[1]}"
    recent = session.get(AppState, key, populate_existing=True)
    retry = bool(recent and recent.value.get("reservation") == str(insight.id))
    if (
        recent
        and not retry
        and datetime.fromisoformat(recent.value["at"]) > now - timedelta(days=7)
    ):
        return False
    if not can_notify(session, settings, now, exclude_insight_key=key if retry else None):
        return False
    upsert(
        session,
        AppState,
        {"key": key, "value": {"at": now.isoformat(), "reservation": str(insight.id)}},
        ["key"],
    )
    return True


@dataclass(frozen=True)
class NotificationDecision:
    action: Literal["allow", "defer", "cancel"]
    reason: str
    policy_revision: str
    retry_after: datetime | None = None


def notification_decision(
    session,
    settings,
    now,
    *,
    include_budget=True,
    exclude_insight_key=None,
    exclude_outbox_id=None,
    snoozed_until=None,
    quiet_retry=None,
    evaluate_quiet=True,
    destination_instance_id=None,
) -> NotificationDecision:
    """Current owner policy shared by legacy and channel-neutral initiatives."""
    state = session.get(AppState, "proactive:enabled", populate_existing=True)
    enabled_now = state.value.get("enabled") if state else settings.proactive_enabled
    revision_input = {
        "owner_control": state.value if state else None,
        "default_enabled": settings.proactive_enabled,
        "timezone": settings.timezone,
        "budget": settings.question_budget,
        "quiet_start": settings.quiet_start_hour,
        "quiet_end": settings.quiet_end_hour,
    }
    revision = hashlib.sha256(json.dumps(revision_input, sort_keys=True).encode()).hexdigest()

    def result(action, reason, retry_after=None):
        return NotificationDecision(action, reason, revision, retry_after)

    if not enabled_now:
        return result("cancel", "owner_paused")
    if snoozed_until is not None and snoozed_until > now:
        return result("defer", "snoozed", snoozed_until)
    if session.scalar(select(TelegramUpdate.id).where(TelegramUpdate.status == "pending").limit(1)):
        return result("defer", "inbound_pending", now + timedelta(minutes=15))
    from garmin_ai.agent import any_pending_clarification, pending_clarification

    if destination_instance_id is None:
        clarification_pending = any_pending_clarification(session, now)
    else:
        previous_destination = session.info.get("channel_destination_instance_id")
        session.info["channel_destination_instance_id"] = destination_instance_id
        try:
            clarification_pending = pending_clarification(session, now) is not None
        finally:
            if previous_destination is None:
                session.info.pop("channel_destination_instance_id", None)
            else:
                session.info["channel_destination_instance_id"] = previous_destination
    if clarification_pending:
        return result("defer", "clarification_pending", now + timedelta(minutes=15))
    if (
        include_budget
        and notification_count(
            session,
            settings,
            now,
            exclude_insight_key=exclude_insight_key,
            exclude_outbox_id=exclude_outbox_id,
        )
        >= settings.question_budget
    ):
        return result("cancel", "daily_budget_exhausted")
    if quiet_retry is None and evaluate_quiet:
        local = now.astimezone(ZoneInfo(settings.timezone))
        start, end = settings.quiet_start_hour, settings.quiet_end_hour
        quiet = (
            (local.hour >= start or local.hour < end) if start > end else start <= local.hour < end
        )
        if quiet:
            target = datetime.combine(local.date(), datetime.min.time(), local.tzinfo).replace(
                hour=end
            )
            if target <= local:
                target += timedelta(days=1)
            quiet_retry = target.astimezone(UTC)
    if quiet_retry is not None:
        return result("defer", "quiet_hours", quiet_retry)
    return result("allow", "allowed")


def can_notify(session, settings, now, *, include_budget=True, exclude_insight_key=None):
    from garmin_ai.accounts import effective_owner_settings

    settings = effective_owner_settings(session, settings)
    return (
        notification_decision(
            session,
            settings,
            now,
            include_budget=include_budget,
            exclude_insight_key=exclude_insight_key,
        ).action
        == "allow"
    )


def generate_insights(session, now, timezone):
    from garmin_ai.scenario_packs import pack_enabled

    session.execute(select(func.pg_advisory_xact_lock(72104621)))
    control = session.get(AppState, "proactive:enabled", populate_existing=True)
    if control is not None and control.value.get("enabled") is False:
        return
    if session.scalar(select(HealthDay.day).limit(1)) is None:
        return
    today = now.astimezone(ZoneInfo(timezone)).date()
    # Exclude the incomplete current day and compare two complete 14-day windows.
    for metric in ("sleep_score", "sleep_seconds", "hrv_nightly_avg", "resting_hr", "stress_avg"):
        pack = "sleep" if metric in {"sleep_score", "sleep_seconds"} else "wellbeing"
        if not pack_enabled(session, pack, "reminders"):
            continue
        key = f"trend:{metric}:{today.isocalendar().year}:{today.isocalendar().week}"
        if session.get(AppState, f"insight:last:{metric}"):
            sent_at = datetime.fromisoformat(
                session.get(AppState, f"insight:last:{metric}").value["at"]
            )
            if sent_at > now - timedelta(days=7):
                continue
        existing = session.scalar(select(Insight).where(Insight.dedup_key == key))
        if existing and (
            existing.status in {"delivered", "uncertain"}
            or (
                existing.status == "cancelled"
                and existing.evidence.get("cancel_reason") == "owner_pause"
            )
        ):
            continue
        result = compare_periods(
            session,
            metric,
            today - timedelta(days=14),
            today - timedelta(days=1),
            today - timedelta(days=28),
            today - timedelta(days=15),
        )
        effect = result["standardized_difference"]
        ci = result["ci95"]
        sufficient = result["a"]["n"] >= 14 and result["b"]["n"] >= 14
        constant_shift = result["a"]["sd"] == result["b"]["sd"] == 0 and result[
            "difference"
        ] not in {None, 0}
        accepted = (
            sufficient
            and ((effect is not None and abs(effect) >= 0.5) or constant_shift)
            and ci is not None
            and ci[0] * ci[1] > 0
        )
        labels = {
            "sleep_score": "Оценка сна",
            "sleep_seconds": "Продолжительность сна (секунды)",
            "hrv_nightly_avg": "Ночной HRV (мс)",
            "resting_hr": "Пульс покоя (уд/мин)",
            "stress_avg": "Средний стресс",
        }
        statement = f"{labels[metric]}: сравнение {result['a']['start']}–{result['a']['end']} и {result['b']['start']}–{result['b']['end']}; разница {result['difference']}; наблюдений {result['a']['n']} и {result['b']['n']}, интервал 95%: {ci}. Связь не доказывает причину."
        session.execute(
            insert(Insight)
            .values(
                category="trend",
                statement=statement,
                evidence=result,
                sample_size=result["a"]["n"] + result["b"]["n"],
                effect_size=effect,
                status="accepted" if accepted else "candidate",
                dedup_key=key,
            )
            .on_conflict_do_update(
                index_elements=[Insight.dedup_key],
                set_={
                    "statement": statement,
                    "evidence": result,
                    "sample_size": result["a"]["n"] + result["b"]["n"],
                    "effect_size": effect,
                    "status": "accepted" if accepted else "candidate",
                    "generated_at": now,
                },
            )
        )


def migraine_question_text(session, e, now):
    medication = session.scalar(
        select(Event.id)
        .where(
            Event.deleted.is_(False),
            Event.kind == "medication",
            Event.status == "confirmed",
            Event.start <= now,
            Event.payload["reason_event_id"].astext == str(e.id),
        )
        .limit(1)
    )
    when = e.start.astimezone(ZoneInfo(e.timezone)).strftime("%d.%m в %H:%M")
    message = f"Мигрень, начавшаяся {when}, уже закончилась? Если да — примерно во сколько?"
    if not medication:
        message += " Принимали ли что-нибудь?"
    reported_severity = session.scalar(
        select(Event.id)
        .where(
            Event.deleted.is_(False),
            Event.status == "confirmed",
            Event.kind == "symptom_observation",
            Event.start <= now,
            Event.payload["episode_id"].astext == str(e.id),
            Event.payload["severity"].as_integer().is_not(None),
        )
        .limit(1)
    )
    if e.payload.get("severity") is None and reported_severity is None:
        message += " Можно также указать силу боли от 0 до 10."
    return message
