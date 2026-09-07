"""Evidence-driven questions with persistent budgets and no automatic repeats."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.analytics import compare_periods
from garmin_ai.models import (
    Activity,
    AppState,
    Event,
    Insight,
    Measurement,
    PendingQuestion,
    TimelineInterval,
)
from garmin_ai.normalize import upsert


def enabled(session, settings):
    state = session.get(AppState, "proactive:enabled")
    return state.value["enabled"] if state else settings.proactive_enabled


def add_question(session, kind, text, evidence, priority, key, now, event_id=None, delay=0):
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


def generate_questions(session, settings, now):
    slot = int(now.timestamp()) // 1800
    state = session.get(AppState, "proactive:generation")
    if state and state.value.get("slot") == slot:
        return
    upsert(session, AppState, dict(key="proactive:generation", value={"slot": slot}), ["key"])
    for e in session.scalars(
        select(Event).where(
            Event.deleted.is_(False),
            Event.kind == "migraine",
            Event.status == "confirmed",
            Event.end.is_(None),
            Event.start <= now - timedelta(hours=2),
            Event.start >= now - timedelta(days=2),
        )
    ):
        medication = session.scalar(
            select(Event.id)
            .where(
                Event.deleted.is_(False),
                Event.kind == "medication",
                Event.payload["reason_event_id"].astext == str(e.id),
            )
            .limit(1)
        )
        when = e.start.astimezone(ZoneInfo(e.timezone)).strftime("%d.%m в %H:%M")
        message = f"Мигрень, начавшаяся {when}, уже закончилась? Если да — примерно во сколько?"
        if not medication:
            message += " Принимали ли что-нибудь?"
        if e.payload.get("severity") is None:
            message += " Можно также указать силу боли от 0 до 10."
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
            Event.start >= now - timedelta(days=14),
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
    if local.hour >= 15 and len(days) >= 7 and local.date() not in days and not ignored:
        add_question(
            session,
            "caffeine",
            "Сегодня кофе был? Если да — примерно когда и сколько?",
            {"logged_days_last_14": len(days)},
            0.6,
            f"caffeine:{local.date()}",
            now,
        )
    # Context questions require a personal reference, not a universal HR threshold.
    hr = session.execute(
        select(Measurement.ts, Measurement.value).where(
            Measurement.metric == "heart_rate_bpm",
            Measurement.ts >= now - timedelta(days=14),
            Measurement.ts < now - timedelta(days=1),
        )
    ).all()
    if len(hr) < 200 or len({r.ts.date() for r in hr}) < 7:
        return
    threshold = float(np.quantile([r.value for r in hr], 0.95))
    recent_stress = session.execute(
        select(Measurement.ts, Measurement.value)
        .where(
            Measurement.metric == "stress_score",
            Measurement.ts >= now - timedelta(hours=3),
            Measurement.ts < now - timedelta(minutes=15),
            Measurement.value >= 85,
        )
        .order_by(Measurement.ts)
    ).all()
    runs = []
    for point in recent_stress:
        if not runs or point.ts - runs[-1][-1] > timedelta(minutes=5):
            runs.append([point.ts])
        else:
            runs[-1].append(point.ts)
    for points in runs:
        if points[-1] - points[0] < timedelta(minutes=20):
            continue
        left, right = points[0], points[-1] + timedelta(minutes=2)
        activity = session.scalar(
            select(Activity.id).where(Activity.start < right, Activity.end > left).limit(1)
        )
        label = session.scalar(
            select(TimelineInterval.id)
            .where(
                TimelineInterval.start < right,
                TimelineInterval.end > left,
                TimelineInterval.confirmed.is_(True),
            )
            .limit(1)
        )
        context = session.scalar(
            select(Event.id)
            .where(
                Event.deleted.is_(False),
                Event.kind == "context",
                Event.start < right,
                Event.end > left,
            )
            .limit(1)
        )
        values = session.scalars(
            select(Measurement.value).where(
                Measurement.metric == "heart_rate_bpm",
                Measurement.ts >= left,
                Measurement.ts < right,
            )
        ).all()
        if activity or label or context or len(values) < 5 or float(np.mean(values)) < threshold:
            continue
        a = left.astimezone(ZoneInfo(settings.timezone))
        b = right.astimezone(ZoneInfo(settings.timezone))
        add_question(
            session,
            "context",
            f"С {a:%H:%M} до {b:%H:%M} были повышены стресс и пульс, а тренировки нет. Чем вы занимались?",
            {
                "start": left.isoformat(),
                "end": right.isoformat(),
                "baseline_hr_p95": threshold,
                "hr_samples": len(values),
                "status": "unknown",
            },
            0.7,
            f"context:{left.isoformat()}",
            now,
        )


def select_question(session, settings, now):
    if not can_notify(session, settings, now):
        return None
    local = now.astimezone(ZoneInfo(settings.timezone))
    start, end = settings.quiet_start_hour, settings.quiet_end_hour
    quiet = (local.hour >= start or local.hour < end) if start > end else start <= local.hour < end
    if quiet:
        return None
    session.execute(select(func.pg_advisory_xact_lock(72104621)))
    day_start = datetime.combine(local.date(), datetime.min.time(), ZoneInfo(settings.timezone))
    sent = session.scalar(
        select(func.count())
        .select_from(PendingQuestion)
        .where(PendingQuestion.sent_at >= day_start)
    )
    if sent >= settings.question_budget:
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
        if q.event_id:
            event = session.get(Event, q.event_id)
            if not event or event.deleted or event.end:
                q.status = "answered"
                continue
        recent = session.scalar(
            select(PendingQuestion.id)
            .where(
                PendingQuestion.kind == q.kind, PendingQuestion.sent_at >= now - timedelta(hours=24)
            )
            .limit(1)
        )
        if recent:
            continue
        q.status = "sending"
        q.sent_at = now
        q.attempts += 1
        return q
    return None


def can_notify(session, settings, now):
    if not enabled(session, settings):
        return False
    hour = now.astimezone(ZoneInfo(settings.timezone)).hour
    start, end = settings.quiet_start_hour, settings.quiet_end_hour
    quiet = (hour >= start or hour < end) if start > end else start <= hour < end
    return not quiet


def generate_insights(session, now, timezone):
    today = now.astimezone(ZoneInfo(timezone)).date()
    # Exclude the incomplete current day and compare two complete 14-day windows.
    for metric in ("sleep_score", "sleep_seconds", "hrv_nightly_avg", "resting_hr", "stress_avg"):
        key = f"trend:{metric}:{today.isocalendar().year}:{today.isocalendar().week}"
        if session.scalar(select(Insight.id).where(Insight.dedup_key == key)):
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
        accepted = (
            sufficient
            and effect is not None
            and abs(effect) >= 0.5
            and ci is not None
            and ci[0] * ci[1] > 0
        )
        statement = f"{metric}: сравнение двух 14-дневных периодов; разница {result['difference']}. Наблюдение, а не причинный вывод."
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
            .on_conflict_do_nothing(index_elements=[Insight.dedup_key])
        )
