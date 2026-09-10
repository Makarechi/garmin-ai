"""Resumable activity scans with overlap validation for mutable offset pages."""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from garmin_ai.jobs import enqueue
from garmin_ai.models import AppState, Job
from garmin_ai.normalize import timestamp, upsert

PAGE_SIZE = 100
OVERLAP = 20


def schedule_scans(session, settings, now):
    binding = session.get(AppState, "account:garmin")
    if not binding:
        return False
    session.execute(select(func.pg_advisory_xact_lock(72104619)))
    account = binding.value["fingerprint"]
    for lane, days in (("recent", 14), ("history", settings.backfill_days)):
        if not days:
            continue
        key = f"activity_scan:{account}:{lane}"
        row = session.get(AppState, key, populate_existing=True)
        previous = row.value if row else {}
        if previous.get("status") == "running":
            # Job retries preserve this cursor; an exhausted job stays visible for repair.
            continue
        if previous.get("next_scan_at") and datetime.fromisoformat(previous["next_scan_at"]) > now:
            continue
        payload = {
            "generation": str(uuid4()),
            "scan_key": key,
            "account": account,
            "offset": 0,
            "round": 0,
            "anchor": [],
            "backfill": lane == "history",
            "since": str(now.astimezone(ZoneInfo(settings.timezone)).date() - timedelta(days=days)),
        }
        job_id = queue_page(session, payload, now)
        upsert(
            session,
            AppState,
            {
                "key": key,
                "value": {
                    **payload,
                    "status": "running",
                    "job_id": str(job_id),
                    "started_at": now.isoformat(),
                    "pages": 0,
                },
            },
            ["key"],
        )
    return True


def queue_page(session, payload, now):
    return enqueue(
        session,
        "garmin_activities",
        payload,
        f"activity-page:{payload['account']}:{payload['generation']}:{payload['round']}:{payload['offset']}",
        now,
    )


def current_page(session, payload):
    if not payload.get("scan_key"):
        return True  # Existing queued jobs remain compatible.
    row = session.get(AppState, payload["scan_key"], populate_existing=True)
    return bool(
        row
        and row.value.get("status") == "running"
        and all(
            row.value.get(field) == payload.get(field)
            for field in ("account", "generation", "round", "offset")
        )
    )


def finish_page(session, payload, values, timezone, now):
    if not payload.get("scan_key"):
        return False
    row = session.get(AppState, payload["scan_key"], populate_existing=True)
    if not current_page(session, payload):
        return True
    ids = [str(value["activityId"]) for value in values]
    anchor = payload["anchor"]
    shifted = bool(anchor and ids[: len(anchor)] != anchor)
    state = {**row.value, "pages": row.value["pages"] + 1, "updated_at": now.isoformat()}
    if shifted:
        if payload["round"] >= 3:
            row.value = {
                **state,
                "status": "unstable_inventory",
                "next_scan_at": (now + timedelta(hours=1)).isoformat(),
            }
            return True
        following = {**payload, "round": payload["round"] + 1, "offset": 0, "anchor": []}
    elif (
        len(values) == PAGE_SIZE
        and timestamp(values[-1]["startTimeGMT"]).astimezone(ZoneInfo(timezone)).date().isoformat()
        >= payload["since"]
    ):
        following = {
            **payload,
            "offset": payload["offset"] + PAGE_SIZE - OVERLAP,
            "anchor": ids[-OVERLAP:],
        }
    else:
        row.value = {
            **state,
            "status": "complete",
            "completed_at": now.isoformat(),
            "next_scan_at": (
                now + (timedelta(days=1) if payload["backfill"] else timedelta(minutes=15))
            ).isoformat(),
        }
        return True
    job_id = queue_page(session, following, now)
    row.value = {**state, **following, "job_id": str(job_id)}
    return True


def scan_status(session):
    result = []
    for row in session.scalars(select(AppState).where(AppState.key.startswith("activity_scan:"))):
        value = {
            key: item
            for key, item in row.value.items()
            if key not in {"account", "anchor", "scan_key"}
        }
        if value.get("status") == "running":
            from uuid import UUID

            job = session.get(Job, UUID(value["job_id"]))
            value["job_status"] = job.status if job else "missing"
        result.append(value)
    return result
