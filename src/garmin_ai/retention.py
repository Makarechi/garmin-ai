"""Explicit transport-text retention; never delete idempotency records or diary facts."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_

from garmin_ai.events import lock_writes
from garmin_ai.models import (
    AppState,
    InboundMessage,
    Job,
    OutboxMessage,
    PendingQuestion,
    TelegramUpdate,
)

REDACTED_REPLY = (
    "Срок хранения технического текста этого ответа истёк. Записи дневника доступны через /history."
)
TERMINAL_NEUTRAL_DELIVERY = {
    "provider_accepted",
    "delivered",
    "read",
    "cancelled",
    "expired",
    "failed",
}


def prune_neutral_text(session, cutoff, now, *, limit, apply, cursor=None):
    inbound_after = None
    orphan_after = None
    inbound_done = False
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise ValueError("Invalid neutral retention cursor")
        decoded = json.loads(cursor)
        if isinstance(decoded, list) and len(decoded) == 2:
            inbound_after = datetime.fromisoformat(decoded[0]), UUID(decoded[1])
        elif isinstance(decoded, dict):
            inbound_done = decoded.get("inbound_done") is True
            if decoded.get("inbound_after") is not None:
                stamp, identity = decoded["inbound_after"]
                inbound_after = datetime.fromisoformat(stamp), UUID(identity)
            if decoded.get("orphan_after") is not None:
                stamp, identity = decoded["orphan_after"]
                orphan_after = datetime.fromisoformat(stamp), UUID(identity)
        else:
            raise ValueError("Invalid neutral retention cursor")
        if any(
            value is not None and value[0].utcoffset() is None
            for value in (inbound_after, orphan_after)
        ):
            raise ValueError("Neutral retention cursor must be aware")
    candidates = (
        []
        if inbound_done
        else session.scalars(
            select(InboundMessage)
            .where(
                InboundMessage.status.in_(["processed", "invalid"]),
                InboundMessage.received_at < cutoff,
                InboundMessage.envelope["_text_redacted"].astext.is_distinct_from("true"),
                tuple_(InboundMessage.received_at, InboundMessage.id) > inbound_after
                if inbound_after
                else True,
            )
            .order_by(InboundMessage.received_at, InboundMessage.id)
            .limit(limit)
            .with_for_update()
        ).all()
    )
    outboxes = session.scalars(
        select(OutboxMessage)
        .where(OutboxMessage.inbound_message_id.in_([row.id for row in candidates]))
        .with_for_update()
    ).all()
    grouped = {}
    for outbox in outboxes:
        grouped.setdefault(outbox.inbound_message_id, []).append(outbox)
    eligible = [
        row
        for row in candidates
        if not grouped.get(row.id)
        or all(item.state in TERMINAL_NEUTRAL_DELIVERY for item in grouped[row.id])
    ]
    remaining = max(0, limit - len(candidates))
    orphan_page = (
        session.scalars(
            select(OutboxMessage)
            .where(
                OutboxMessage.inbound_message_id.is_(None),
                OutboxMessage.created_at < cutoff,
                OutboxMessage.state.in_(TERMINAL_NEUTRAL_DELIVERY),
                OutboxMessage.intent["_text_redacted"].astext.is_distinct_from("true"),
                tuple_(OutboxMessage.created_at, OutboxMessage.id) > orphan_after
                if orphan_after
                else True,
            )
            .order_by(OutboxMessage.created_at, OutboxMessage.id)
            .limit(remaining + 1)
            .with_for_update()
        ).all()
        if remaining
        else []
    )
    orphan_more = len(orphan_page) > remaining
    orphaned_outboxes = orphan_page[:remaining]
    if apply:
        for row in eligible:
            receipt = str(uuid4())
            row.normalized_text = None
            row.envelope = {
                "_text_redacted": True,
                "receipt": receipt,
                "redacted_at": now.isoformat(),
            }
            for outbox in grouped.get(row.id, []):
                outbox.intent = {
                    "_text_redacted": True,
                    "receipt": receipt,
                    "redacted_at": now.isoformat(),
                }
        for outbox in orphaned_outboxes:
            outbox.intent = {
                "_text_redacted": True,
                "receipt": str(uuid4()),
                "redacted_at": now.isoformat(),
            }
    inbound_more = not inbound_done and len(candidates) == limit
    next_cursor = None
    if inbound_more or orphan_more:
        if candidates:
            inbound_after = candidates[-1].received_at, candidates[-1].id
        if orphaned_outboxes:
            orphan_after = orphaned_outboxes[-1].created_at, orphaned_outboxes[-1].id
        next_cursor = json.dumps(
            {
                "inbound_after": (
                    [inbound_after[0].isoformat(), str(inbound_after[1])] if inbound_after else None
                ),
                "inbound_done": not inbound_more,
                "orphan_after": (
                    [orphan_after[0].isoformat(), str(orphan_after[1])] if orphan_after else None
                ),
            }
        )
    return (
        len(candidates) + len(orphaned_outboxes),
        len(eligible) + len(orphaned_outboxes),
        next_cursor,
    )


def prune_telegram_text(
    session,
    *,
    older_than_days=90,
    limit=1000,
    apply=False,
    now=None,
    cursor=None,
    answer_cursor=None,
    neutral_cursor=None,
):
    if not 30 <= older_than_days <= 3650 or not 1 <= limit <= 1000:
        raise ValueError("Retention requires 30–3650 days and a batch of 1–1000 updates")
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("Retention clock must be aware")
    cutoff = now - timedelta(days=older_than_days)
    answer_after = UUID(answer_cursor) if answer_cursor is not None else None
    after = None
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise ValueError("Invalid retention cursor")
        stamp, identity = json.loads(cursor)
        after = datetime.fromisoformat(stamp), int(identity)
        if after[0].utcoffset() is None:
            raise ValueError("Retention cursor must be aware")
    lock_writes(session)
    neutral_scanned, neutral_eligible, next_neutral_cursor = prune_neutral_text(
        session, cutoff, now, limit=limit, apply=apply, cursor=neutral_cursor
    )
    # CLI also holds standalone file locks: the worker cannot claim/retry while pruning.
    candidates = session.scalars(
        select(TelegramUpdate)
        .where(
            TelegramUpdate.status.in_(["processed", "invalid"]),
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
    pending_rows = session.scalars(
        select(AppState).where(AppState.key.startswith("conversation:pending"))
    ).all()
    expired_pending = 0
    for pending in pending_rows:
        if not isinstance(pending.value, dict):
            continue
        try:
            created = datetime.fromisoformat(pending.value["created_at"])
            expired = created.utcoffset() is not None and created < cutoff
        except (KeyError, TypeError, ValueError):
            expired = False  # Unknown age is not authority to delete a clarification.
        if expired:
            expired_pending += 1
            if apply:
                session.delete(pending)
    expired_answers = 0
    answers = session.scalars(
        select(PendingQuestion)
        .where(
            PendingQuestion.evidence["answer_text"].astext.is_not(None),
            PendingQuestion.id > answer_after if answer_after else True,
        )
        .order_by(PendingQuestion.id)
        .limit(limit)
        .with_for_update()
    ).all()
    for question in answers:
        try:
            answered = datetime.fromisoformat(question.evidence["answered_at"])
            old_answer = answered.utcoffset() is not None and answered < cutoff
        except (KeyError, TypeError, ValueError):
            continue
        if old_answer:
            expired_answers += 1
            if apply:
                question.evidence = {
                    key: value for key, value in question.evidence.items() if key != "answer_text"
                }
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
            "_channel_instance": update.payload.get(
                "_channel_instance", {"channel": "telegram", "instance_id": "primary"}
            ),
            "update_id": update.payload.get("update_id", update.id),
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
        "eligible_clarifications": expired_pending,
        "eligible_proactive_answers": expired_answers,
        "scanned_neutral_messages": neutral_scanned,
        "eligible_neutral_messages": neutral_eligible,
        "next_neutral_cursor": next_neutral_cursor,
        "scanned_proactive_answers": len(answers),
        "next_answer_cursor": str(answers[-1].id) if len(answers) == limit else None,
        "batch_limit_reached": len(candidates) == limit,
        "next_cursor": json.dumps([candidates[-1].received_at.isoformat(), candidates[-1].id])
        if len(candidates) == limit
        else None,
    }
