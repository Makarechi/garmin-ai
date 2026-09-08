"""Evidence-driven questions with persistent budgets and no automatic repeats."""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.analytics import compare_periods
from garmin_ai.events import reactivate_question
from garmin_ai.models import (
    Activity,
    AppState,
    Event,
    Insight,
    Measurement,
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


def context_explained(session, left, right):
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
            Event.kind.in_(CONTEXT_KINDS),
            Event.status == "confirmed",
            Event.start < right,
            or_(Event.end > left, Event.end.is_(None) & (Event.start >= left)),
        )
        .limit(1)
    )
    return bool(activity or label or context)


def personal_hr_threshold(session, timezone, now):
    hr = session.execute(
        select(Measurement.ts, Measurement.value).where(
            Measurement.metric == "heart_rate_bpm",
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
    values = session.scalars(
        select(Measurement.value).where(
            Measurement.metric == "heart_rate_bpm",
            Measurement.ts >= left,
            Measurement.ts < right,
        )
    ).all()
    if len(values) < 5 or float(np.mean(values)) < threshold:
        return None
    return {"baseline_hr_p95": threshold, "hr_samples": len(values)}


def generate_questions(session, settings, now, *, allow_context=True):
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
            PendingQuestion.status.in_(["sent", "uncertain", "answered", "acknowledged"]),
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
        local.hour >= 15
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
    if not allow_context:
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
            f"{a:%d.%m.%Y} с {a:%H:%M} до {ending} были повышены стресс и пульс, а тренировки нет. Чем вы занимались?",
            {
                "start": left.isoformat(),
                "end": right.isoformat(),
                **evidence,
                "timezone": settings.timezone,
                "status": "unknown",
            },
            0.7,
            f"context:{left.isoformat()}",
            now,
        )


def reconcile_answers(session, now):
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
            answer = session.scalar(
                select(Event)
                .where(
                    Event.deleted.is_(False),
                    Event.status == "confirmed",
                    Event.kind.in_(CONTEXT_KINDS),
                    Event.start < right,
                    or_(Event.end > left, (Event.end.is_(None) & (Event.start >= left))),
                )
                .order_by(Event.start)
                .limit(1)
            )
            if answer is None and context_explained(session, left, right):
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


def select_question(session, settings, now, *, allow_context=True):
    session.execute(select(func.pg_advisory_xact_lock(72104621)))
    from garmin_ai.agent import pending_clarification

    if pending_clarification(session, now) or session.scalar(
        select(TelegramUpdate.id).where(TelegramUpdate.status == "pending").limit(1)
    ):
        return None
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
            if evidence is None or context_explained(session, left, right):
                q.status = "cancelled"
                continue
            q.evidence = {**q.evidence, **evidence}
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


def can_notify(session, settings, now):
    if (
        session.scalar(select(TelegramUpdate.id).where(TelegramUpdate.status == "pending").limit(1))
        is not None
    ):
        return False
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
        if session.get(AppState, f"insight:last:{metric}"):
            sent_at = datetime.fromisoformat(
                session.get(AppState, f"insight:last:{metric}").value["at"]
            )
            if sent_at > now - timedelta(days=7):
                continue
        existing = session.scalar(select(Insight).where(Insight.dedup_key == key))
        if existing and existing.status in {"delivered", "uncertain"}:
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
    if e.payload.get("severity") is None:
        message += " Можно также указать силу боли от 0 до 10."
    return message
