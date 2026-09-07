"""PostgreSQL-backed leased jobs; handlers must be idempotent."""

import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.models import Job


def enqueue(session, kind: str, payload: dict, dedup_key: str, run_at: datetime):
    return session.scalar(
        insert(Job)
        .values(kind=kind, payload=payload, dedup_key=dedup_key, run_at=run_at)
        .on_conflict_do_nothing(index_elements=[Job.dedup_key])
        .returning(Job.id)
    )


def claim(
    session,
    *,
    now: datetime | None = None,
    lease_seconds: int = 300,
    kinds: list[str] | None = None,
):
    now = now or datetime.now(UTC)
    expired = and_(Job.status == "running", Job.lease_until < now)
    session.execute(
        update(Job)
        .where(Job.attempts >= 8, or_(expired, Job.status == "pending"))
        .values(
            status="failed", last_error="RetryLimitExceeded", lease_until=None, lease_token=None
        )
    )
    row = session.scalar(
        select(Job)
        .where(
            Job.kind.in_(kinds) if kinds is not None else True,
            Job.attempts < 8,
            or_(
                and_(Job.status == "pending", Job.run_at <= now),
                and_(Job.status == "running", Job.lease_until < now),
            ),
        )
        .order_by(Job.run_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    row.status = "running"
    row.lease_until = now + timedelta(seconds=lease_seconds)
    row.lease_token = uuid.uuid4()
    row.attempts += 1
    session.flush()
    return row


def renew(session, job_id, lease_token, *, now: datetime | None = None):
    now = now or datetime.now(UTC)
    result = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == "running",
            Job.lease_token == lease_token,
            Job.lease_until > now,
        )
        .values(lease_until=func.greatest(Job.lease_until, now + timedelta(minutes=5)))
    )
    return result.rowcount == 1


def finish(session, job_id, lease_token, *, error_type: str | None = None):
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
    )
    if row is None:
        raise ValueError("Job lease expired or belongs to a different worker")
    row.lease_until = None
    row.lease_token = None
    if error_type:
        row.status = "failed" if row.attempts >= 8 else "pending"
        row.last_error = error_type
        row.run_at = now + timedelta(
            seconds=min(3600, 15 * 2**row.attempts) + random.uniform(0, 10)
        )
    else:
        row.status = "done"
        row.completed_at = now
        row.last_error = None
