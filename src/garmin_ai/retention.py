"""Explicit transport-text retention; never delete idempotency records or diary facts."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, tuple_

from garmin_ai.events import lock_writes
from garmin_ai.models import AppState, Job, TelegramUpdate

REDACTED_REPLY = (
    "Срок хранения технического текста этого ответа истёк. Записи дневника доступны через /history."
)


def prune_telegram_text(
    session, *, older_than_days=90, limit=1000, apply=False, now=None, cursor=None
):
    if not 30 <= older_than_days <= 3650 or not 1 <= limit <= 1000:
        raise ValueError("Retention requires 30–3650 days and a batch of 1–1000 updates")
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("Retention clock must be aware")
    cutoff = now - timedelta(days=older_than_days)
    after = None
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise ValueError("Invalid retention cursor")
        stamp, identity = json.loads(cursor)
        after = datetime.fromisoformat(stamp), int(identity)
        if after[0].utcoffset() is None:
            raise ValueError("Retention cursor must be aware")
    lock_writes(session)
    # CLI also holds standalone file locks: the worker cannot claim/retry while pruning.
    candidates = session.scalars(
        select(TelegramUpdate)
        .where(
            TelegramUpdate.status == "processed",
            TelegramUpdate.received_at < cutoff,
            TelegramUpdate.payload["_text_redacted"].astext.is_distinct_from("true"),
            tuple_(TelegramUpdate.received_at, TelegramUpdate.id) > after if after else True,
        )
        .order_by(TelegramUpdate.received_at, TelegramUpdate.id)
        .limit(limit)
        .with_for_update()
    ).all()
    grouped_jobs = {}
    if candidates:
        related_jobs = session.scalars(
            select(Job)
            .where(
                Job.payload["update_id"].astext.in_([str(item.id) for item in candidates]),
                Job.kind.in_(
                    ["telegram_update", "telegram_control", "telegram_ack", "telegram_failure"]
                ),
            )
            .with_for_update()
        ).all()
        for job in related_jobs:
            grouped_jobs.setdefault(str(job.payload["update_id"]), []).append(job)
    pending = session.get(AppState, "conversation:pending")
    expired_pending = False
    if pending and isinstance(pending.value, dict):
        try:
            created = datetime.fromisoformat(pending.value["created_at"])
            expired_pending = created.utcoffset() is not None and created < cutoff
        except (KeyError, TypeError, ValueError):
            pass  # Unknown age is not authority to delete a clarification.
    if apply and expired_pending:
        session.delete(pending)
    eligible = []
    job_count = 0
    transcript_count = 0
    for update in candidates:
        if not isinstance(update.payload, dict):
            continue
        jobs = grouped_jobs.get(str(update.id), [])
        primary = next((job for job in jobs if job.dedup_key == f"telegram:{update.id}"), None)
        reply = session.get(AppState, f"telegram:reply:{update.id}")
        if (
            not primary
            or not reply
            or any(job.status != "done" for job in jobs)
            or any(job.completed_at is None or job.completed_at >= cutoff for job in jobs)
        ):
            continue
        transcript = session.get(AppState, f"telegram:transcript:{update.id}")
        transcript_count += int(transcript is not None)
        eligible.append(update.id)
        job_count += len(jobs)
        if not apply:
            continue
        # The update ID and received_at remain; duplicate delivery cannot re-enqueue.
        update.payload = {
            "_text_redacted": True,
            "_ordering_epoch": update.payload.get("_ordering_epoch", 0),
            "receipt": str(uuid4()),
            "redacted_at": now.isoformat(),
        }
        if transcript is not None:
            transcript.value = {"text": "", "_text_redacted": True}
        for job in jobs:
            job.payload = {"update_id": update.id, "_text_redacted": True}
        # Preserve the reply key so direct replay returns a tombstone, not a new parse.
        reply.value = {
            "text": REDACTED_REPLY,
            "status": "redacted",
            "keyboard": False,
            "kind": reply.value.get("kind", "diary"),
        }
    if apply:
        session.flush()
    return {
        "applied": apply,
        "cutoff": cutoff.isoformat(),
        "scanned": len(candidates),
        "eligible_updates": len(eligible),
        "eligible_jobs": job_count,
        "eligible_transcripts": transcript_count,
        "eligible_clarifications": int(expired_pending),
        "batch_limit_reached": len(candidates) == limit,
        "next_cursor": json.dumps([candidates[-1].received_at.isoformat(), candidates[-1].id])
        if len(candidates) == limit
        else None,
    }
