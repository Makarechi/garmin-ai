"""PostgreSQL-backed leased jobs; handlers must be idempotent."""

import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, String, and_, cast, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from garmin_ai.models import AppState, Job, TelegramUpdate


def enqueue(session, kind: str, payload: dict, dedup_key: str, run_at: datetime):
    if run_at.tzinfo is None or run_at.utcoffset() is None:
        raise ValueError("Job schedule must include a timezone")
    run_at = run_at.astimezone(UTC)
    return session.scalar(
        insert(Job)
        .values(kind=kind, payload=payload, dedup_key=dedup_key, run_at=run_at)
        .on_conflict_do_nothing(index_elements=[Job.dedup_key])
        .returning(Job.id)
    )


def telegram_order():
    return tuple_(
        func.coalesce(cast(Job.payload["ordering_epoch"].astext, BigInteger), 0),
        cast(Job.payload["update_id"].astext, BigInteger),
    )


def failed_context_sync(session, now):
    """Keep recent failed evidence in the cycle until that feed has been refreshed."""
    failures = []
    for dependency in session.scalars(
        select(Job).where(
            Job.status == "failed",
            func.coalesce(Job.completed_at, Job.run_at) >= now - timedelta(hours=3),
            or_(
                Job.kind == "garmin_activities",
                (Job.kind == "garmin_endpoint")
                & Job.payload["endpoint"].as_string().in_(["heart_rate", "stress"]),
            ),
        )
    ):
        payload = dependency.payload
        key = (
            f"freshness:activities:page:{payload.get('offset', 0)}"
            if dependency.kind == "garmin_activities"
            else f"freshness:{payload['endpoint']}:{payload.get('key', '')}"
        )
        refreshed = session.get(AppState, key, populate_existing=True)
        success = refreshed.value.get("success_at") if refreshed else None
        if success is None or datetime.fromisoformat(success) < (
            dependency.completed_at or dependency.run_at
        ):
            failures.append(str(dependency.id))
    return failures


def claim(
    session,
    *,
    now: datetime | None = None,
    lease_seconds: int = 300,
    kinds: list[str] | None = None,
):
    if not 1 <= lease_seconds <= 86400:
        raise ValueError("Lease duration must be between one second and one day")
    now = now or datetime.now(UTC)
    if (kinds is None or "telegram_update" in kinds) and not session.scalar(
        text("SELECT pg_try_advisory_xact_lock(72104623)")
    ):
        return None
    if (
        kinds is None
        or set(kinds) & {"backup", "garmin_endpoint", "garmin_activities", "garmin_fit"}
    ) and not session.scalar(text("SELECT pg_try_advisory_xact_lock(72104624)")):
        return None
    expired = and_(Job.status == "running", Job.lease_until < now)
    exhausted = session.scalars(
        select(Job.id)
        .where(Job.attempts >= 8, or_(expired, Job.status == "pending"))
        .with_for_update(skip_locked=True)
        .limit(100)
    ).all()
    session.execute(
        update(Job)
        .where(Job.id.in_(exhausted))
        .values(
            status="failed",
            last_error="RetryLimitExceeded",
            completed_at=now,
            lease_until=None,
            lease_token=None,
        )
    )
    applied = (
        select(TelegramUpdate.id)
        .where(
            TelegramUpdate.id == cast(Job.payload["update_id"].astext, BigInteger),
            TelegramUpdate.status != "pending",
        )
        .exists()
    )
    oldest_update = (
        select(Job.id)
        .where(Job.kind == "telegram_update", Job.status.in_(["pending", "running"]), ~applied)
        .order_by(telegram_order())
        .limit(1)
        .correlate(None)
        .scalar_subquery()
    )
    dependency = aliased(Job)
    unfinished_sync = (
        select(dependency.id)
        .where(
            dependency.kind.in_(["garmin_endpoint", "garmin_activities", "garmin_fit"]),
            Job.payload["sync_dependencies"].contains(
                func.jsonb_build_array(cast(dependency.id, String))
            ),
            dependency.status.in_(["pending", "running"]),
        )
        .exists()
    )
    activity_pending = (
        select(dependency.id)
        .where(
            or_(
                dependency.kind == "garmin_activities",
                (dependency.kind == "garmin_endpoint")
                & dependency.payload["endpoint"].as_string().in_(["heart_rate", "stress"]),
            ),
            dependency.status.in_(["pending", "running"]),
        )
        .exists()
    )
    backup_sync_pending = (
        select(dependency.id)
        .where(
            dependency.kind.in_(["garmin_endpoint", "garmin_activities", "garmin_fit"]),
            dependency.status.in_(["pending", "running"]),
        )
        .exists()
    )
    backup_running = (
        select(dependency.id)
        .where(dependency.kind == "backup", dependency.status == "running")
        .exists()
    )
    row = session.scalar(
        select(Job)
        .where(
            or_(
                ~Job.kind.in_(["garmin_endpoint", "garmin_activities", "garmin_fit"]),
                ~backup_running,
            ),
            Job.kind.in_(kinds) if kinds is not None else True,
            Job.attempts < 8,
            or_(Job.kind != "agent_insights", ~unfinished_sync),
            or_(Job.kind != "backup", ~backup_sync_pending),
            or_(Job.kind != "agent_proactive", ~activity_pending),
            or_(
                Job.kind != "telegram_update",
                applied,
                Job.id == oldest_update,
                Job.payload["safety_checked"].as_boolean().is_(False),
            ),
            or_(
                and_(Job.status == "pending", Job.run_at <= now),
                and_(Job.status == "running", Job.lease_until < now),
            ),
        )
        .order_by(Job.run_at)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
        .limit(1)
    )
    if row is None:
        return None
    if row.kind == "agent_proactive":
        row.payload = {**row.payload, "context_sync_failures": failed_context_sync(session, now)}
    row.status = "running"
    row.lease_until = now + timedelta(seconds=lease_seconds)
    row.lease_token = uuid.uuid4()
    row.attempts += 1
    session.flush()
    return row


def renew(session, job_id, lease_token, *, now: datetime | None = None, lease_seconds: int = 300):
    """Renew ownership; callers using custom claim leases must pass the same duration."""
    if lease_seconds < 1:
        raise ValueError("Lease duration must be positive")
    now = now or datetime.now(UTC)
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == "running",
            Job.lease_token == lease_token,
            Job.lease_until > now,
        )
        .values(lease_until=func.greatest(Job.lease_until, now + timedelta(seconds=lease_seconds)))
    )
    return result.rowcount == 1


def finish(
    session, job_id, lease_token, *, error_type: str | None = None, retryable_delivery=False
):
    now = datetime.now(UTC)
    row = session.scalar(
        select(Job)
        .where(
            Job.id == job_id,
            Job.status == "running",
            Job.lease_token == lease_token,
            Job.lease_until > now,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ValueError("Job lease expired or belongs to a different worker")
    row.lease_until = None
    row.lease_token = None
    if error_type:
        if retryable_delivery or error_type == "DiaryDeferred":
            row.attempts = max(0, row.attempts - 1)
        row.status = "failed" if row.attempts >= 8 else "pending"
        row.last_error = error_type
        row.completed_at = now if row.status == "failed" else None
        row.run_at = (
            now
            if row.status == "failed"
            else now + timedelta(seconds=min(3600, 15 * 2**row.attempts) + random.uniform(0, 10))
        )
    else:
        row.status = "done"
        row.completed_at = now
        row.last_error = None
