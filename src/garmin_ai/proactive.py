"""Evidence-driven questions with persistent budgets and no automatic repeats."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import BigInteger, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.analytics import compare_periods
from garmin_ai.models import (
    Activity,
    AppState,
    Event,
    Insight,
    Job,
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
                PendingQuestion.kind == "context", PendingQuestion.expires_at > now
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
                Event.status == "confirmed",
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
            or_(Event.start >= left, Event.end > left),
            Event.start < left + timedelta(days=1),
            Event.start <= now,
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
        values = session.scalars(
            select(Measurement.value).where(
                Measurement.metric == "heart_rate_bpm",
                Measurement.ts >= left,
                Measurement.ts < right,
            )
        ).all()
        if (
            context_explained(session, left, right)
            or len(values) < 5
            or float(np.mean(values)) < threshold
        ):
            continue
        a = left.astimezone(ZoneInfo(settings.timezone))
        b = right.astimezone(ZoneInfo(settings.timezone))
        add_question(
            session,
            "context",
            f"{a:%d.%m.%Y} с {a:%H:%M} до {b:%H:%M} были повышены стресс и пульс, а тренировки нет. Чем вы занимались?",
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


def reconcile_answers(session, now):
    for question in session.scalars(
        select(PendingQuestion).where(
            PendingQuestion.expires_at >= now - timedelta(days=7),
            PendingQuestion.status.in_(
                ["pending", "sent", "uncertain", "answered", "acknowledged", "cancelled"]
            ),
        )
    ):
        answer = None
        if question.kind == "migraine" and question.event_id:
            episode = session.get(Event, question.event_id, populate_existing=True)
            if (
                not episode
                or episode.deleted
                or episode.kind != "migraine"
                or episode.status != "confirmed"
            ):
                question.status = "cancelled"
                continue
            answer = episode if episode.end else None
        elif question.kind == "caffeine" and question.evidence.get("day"):
            zone = ZoneInfo(question.evidence.get("timezone", "Europe/Bratislava"))
            left = datetime.fromisoformat(question.evidence["day"]).replace(tzinfo=zone)
            answer = session.scalar(
                select(Event)
                .where(
                    Event.deleted.is_(False),
                    Event.status == "confirmed",
                    Event.kind.in_(["caffeine", "caffeine_absence"]),
                    or_(
                        Event.start >= left, (Event.kind == "caffeine_absence") & (Event.end > left)
                    ),
                    Event.start < left + timedelta(days=1),
                    Event.start <= now,
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
        else:
            continue
        if answer:
            question.status = "answered"
            question.evidence = {**question.evidence, "answer_event_id": str(answer.id)}
        elif question.status == "answered" or (
            question.status == "cancelled" and question.kind == "migraine"
        ):
            # Restore unanswered conversation context without repeating a delivered prompt.
            question.status = "sent" if question.sent_at else "pending"
            question.evidence = {
                k: v for k, v in question.evidence.items() if k != "answer_event_id"
            }


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


def select_question(session, settings, now):
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
        if q.event_id:
            event = session.get(Event, q.event_id, populate_existing=True)
            if (
                not event
                or event.deleted
                or event.kind != "migraine"
                or event.status != "confirmed"
            ):
                q.status = "cancelled"
                continue
            if event.end:
                q.status = "answered"
                continue
        if q.kind == "context" and q.evidence.get("start") and q.evidence.get("end"):
            if context_explained(
                session,
                datetime.fromisoformat(q.evidence["start"]),
                datetime.fromisoformat(q.evidence["end"]),
            ):
                q.status = "cancelled"
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
        q.expires_at = now + timedelta(days=2)
        q.attempts += 1
        return q
    return None


def can_notify(session, settings, now):
    if (
        session.scalar(
            select(TelegramUpdate.id)
            .join(Job, cast(Job.payload["update_id"].as_string(), BigInteger) == TelegramUpdate.id)
            .where(TelegramUpdate.status == "pending", Job.kind == "telegram_control")
            .limit(1)
        )
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
