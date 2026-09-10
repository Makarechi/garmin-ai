"""Durable daily history windows scoped to the enrolled Garmin owner."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import String, case, cast, func, select

from garmin_ai.garmin import ENDPOINTS
from garmin_ai.jobs import enqueue
from garmin_ai.models import AppState, Job
from garmin_ai.normalize import upsert


def schedule_history(session, settings, now):
    binding = session.get(AppState, "account:garmin")
    if not binding or not settings.backfill_days:
        return
    session.execute(select(func.pg_advisory_xact_lock(72104619)))
    account = binding.value["fingerprint"]
    yesterday = now.astimezone(ZoneInfo(settings.timezone)).date() - timedelta(days=1)
    key = f"syncplan:daily:{account}:{settings.backfill_days}"
    plan = session.get(AppState, key, populate_existing=True)
    state = (
        dict(plan.value)
        if plan
        else {
            "account": account,
            "history_start": str(yesterday - timedelta(days=settings.backfill_days - 1)),
            "history_next": str(yesterday),
            "recent_through": str(yesterday),
            "created_at": now.isoformat(),
            "horizon_days": settings.backfill_days,
        }
    )

    def schedule_day(day):
        for endpoint in ENDPOINTS:
            if endpoint.scope != "day":
                continue
            window = f"syncwindow:{account}:{endpoint.name}:{day}"
            if session.get(AppState, window) is not None:
                continue
            job_id = enqueue(
                session,
                "garmin_endpoint",
                {
                    "endpoint": endpoint.name,
                    "key": str(day),
                    "backfill": True,
                    "account": account,
                    "sync_window": window,
                },
                window,
                now,
            )
            # Enqueue and cursor advancement commit atomically.
            upsert(
                session,
                AppState,
                {
                    "key": window,
                    "value": {
                        "endpoint": endpoint.name,
                        "date": str(day),
                        "status": "pending",
                        "job_id": str(job_id) if job_id else None,
                        "account": account,
                    },
                },
                ["key"],
            )

    # At most two source days per scheduler pass, with outage recovery first.
    budget = 2
    recent = date.fromisoformat(state["recent_through"])
    while recent < yesterday and budget:
        recent += timedelta(days=1)
        schedule_day(recent)
        budget -= 1
    state["recent_through"] = str(recent)
    cursor, left = (
        date.fromisoformat(state["history_next"]),
        date.fromisoformat(state["history_start"]),
    )
    while cursor >= left and budget:
        schedule_day(cursor)
        cursor -= timedelta(days=1)
        budget -= 1
    state["history_next"] = str(cursor)
    state["scheduling_complete"] = cursor < left and recent == yesterday
    upsert(session, AppState, {"key": key, "value": state}, ["key"])


def complete_window(session, payload, result, now):
    key = payload.get("sync_window")
    if not key:
        return
    row = session.get(AppState, key, populate_existing=True)
    if row:
        row.value = {
            **row.value,
            "status": result["status"],
            "source_ref": result.get("source_ref"),
            "completed_at": now.isoformat(),
        }


def history_status(session):
    status = case(
        (
            AppState.value["status"].as_string() == "pending",
            func.coalesce(Job.status, "needs_attention"),
        ),
        else_=AppState.value["status"].as_string(),
    )
    counts = dict(
        session.execute(
            select(status, func.count())
            .select_from(AppState)
            .outerjoin(Job, cast(Job.id, String) == AppState.value["job_id"].as_string())
            .where(AppState.key.startswith("syncwindow:"))
            .group_by(status)
        ).all()
    )
    return {
        "windows": counts,
        "account_first_day": "unknown",
        "plans": [
            {key: value for key, value in row.value.items() if key != "account"}
            for row in session.scalars(
                select(AppState).where(AppState.key.startswith("syncplan:daily:"))
            )
        ],
        "semantics": "requested daily windows, not percentage of all account history",
    }
