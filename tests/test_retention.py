from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState, Audit, Event, Job, TelegramUpdate
from garmin_ai.retention import REDACTED_REPLY, prune_telegram_text
from garmin_ai.telegram import process_message, save_update

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def seed(db, identity=1, status="done", completed=None):
    old = NOW - timedelta(days=100)
    payload = {
        "update_id": identity,
        "message": {
            "message_id": identity,
            "date": old.isoformat(),
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42},
            "text": "synthetic private transport text",
        },
    }
    db.add(TelegramUpdate(id=identity, payload=payload, received_at=old, status="processed"))
    db.add(
        Job(
            kind="telegram_update",
            dedup_key=f"telegram:{identity}",
            payload={"update_id": identity, "transcript": "synthetic private transcript"},
            status=status,
            run_at=old,
            completed_at=completed or old,
        )
    )
    db.add(
        AppState(
            key=f"telegram:reply:{identity}",
            value={
                "text": "synthetic private answer",
                "status": "pending",
                "keyboard": {"synthetic": "private"},
            },
        )
    )
    db.add(AppState(key=f"outbox:update:{identity}:0", value={"status": "sent", "message_id": 88}))
    db.flush()
    return payload


def test_preview_and_apply_preserve_replay_receipts_diary_and_audit(db, db_engine):
    payload = seed(db)
    event = create_event(
        db,
        EventInput(
            start=NOW,
            payload={"type": "medication", "name": "synthetic", "dose": 1, "unit": "tablet"},
            original_text="diary source retained",
        ),
        actor="owner",
    )
    before_audits = db.scalar(select(func.count()).select_from(Audit))
    preview = prune_telegram_text(db, now=NOW)
    assert preview["eligible_updates"] == 1 and not preview["applied"]
    assert db.get(TelegramUpdate, 1).payload == payload
    assert "private" in db.get(AppState, "telegram:reply:1").value["text"]
    result = prune_telegram_text(db, now=NOW, apply=True)
    assert result["eligible_updates"] == 1
    assert "private" not in str(db.get(TelegramUpdate, 1).payload)
    assert "private" not in str(db.scalar(select(Job)).payload)
    assert db.get(Event, event.id).original_text == "diary source retained"
    assert db.scalar(select(func.count()).select_from(Audit)) == before_audits
    db.commit()
    assert save_update(db, payload, 42)
    db.commit()
    assert process_message(db_engine, None, Settings(telegram_user_id=42), 1) == REDACTED_REPLY
    assert db.scalar(select(func.count()).select_from(Job)) == 1
    assert db.scalar(select(func.count()).select_from(Event)) == 1
    assert db.get(AppState, "outbox:update:1:0").value["status"] == "sent"
    import asyncio

    from garmin_ai.telegram import deliver

    class Bot:
        async def send_message(self, **kwargs):
            pytest.fail("A pruned, already sent reply must not be sent again")

    asyncio.run(deliver(Bot(), db_engine, 42, "update:1", REDACTED_REPLY))
    assert prune_telegram_text(db, now=NOW, apply=True)["eligible_updates"] == 0


@pytest.mark.parametrize("status", ["pending", "running", "failed"])
def test_unfinished_jobs_keep_transport_text(db, status):
    seed(db, status=status)
    assert prune_telegram_text(db, now=NOW, apply=True)["eligible_updates"] == 0
    assert "private" in str(db.get(TelegramUpdate, 1).payload)


def test_recent_completion_and_pending_update_are_not_pruned(db):
    seed(db, completed=NOW)
    seed(db, identity=2)
    db.get(TelegramUpdate, 2).status = "pending"
    assert prune_telegram_text(db, now=NOW, apply=True)["eligible_updates"] == 0


def test_cursor_progresses_past_ineligible_older_rows(db):
    seed(db, status="failed")
    seed(db, identity=2)
    first = prune_telegram_text(db, now=NOW, limit=1, apply=True)
    assert first["eligible_updates"] == 0 and first["next_cursor"]
    second = prune_telegram_text(db, now=NOW, limit=1, cursor=first["next_cursor"], apply=True)
    assert second["eligible_updates"] == 1


@pytest.mark.parametrize("days", [-1, 0, 29, 3651])
def test_short_or_unbounded_horizon_rejected(db, days):
    with pytest.raises(ValueError):
        prune_telegram_text(db, older_than_days=days, now=NOW)


def test_cli_preview_then_explicit_apply(db, db_engine, tmp_path, monkeypatch, capsys):
    import json
    import sys

    from garmin_ai import cli
    from garmin_ai import db as database

    seed(db)
    db.commit()
    config = Settings(
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
        backup_dir=tmp_path / "backups",
    )
    monkeypatch.setattr(cli, "Settings", lambda: config)
    monkeypatch.setattr(database, "make_engine", lambda _: db_engine)
    monkeypatch.setattr(sys, "argv", ["garmin-ai", "prune-telegram-text"])
    cli.main()
    assert not json.loads(capsys.readouterr().out)["applied"]
    monkeypatch.setattr(sys, "argv", ["garmin-ai", "prune-telegram-text", "--apply"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] and result["eligible_updates"] == 1


def test_cached_voice_text_is_redacted_atomically_and_never_retranscribed(
    db, db_engine, monkeypatch
):
    import asyncio

    from garmin_ai import runtime

    seed(db)
    db.add(AppState(key="telegram:transcript:1", value={"text": "synthetic private voice"}))
    db.flush()
    assert prune_telegram_text(db, now=NOW)["eligible_transcripts"] == 1
    assert db.get(AppState, "telegram:transcript:1").value["text"] == "synthetic private voice"
    prune_telegram_text(db, now=NOW, apply=True)
    assert db.get(AppState, "telegram:transcript:1").value == {"text": "", "_text_redacted": True}
    db.commit()

    async def forbidden(*args):
        pytest.fail("Pruned transcripts must not be downloaded or sent to a model again")

    monkeypatch.setattr(runtime, "transcribe_voice", forbidden)
    assert asyncio.run(runtime.cached_transcription(db_engine, None, None, {}, 1)) == ""
    assert process_message(db_engine, None, Settings(telegram_user_id=42), 1) == REDACTED_REPLY


@pytest.mark.parametrize("days,eligible", [(100, True), (1, False)])
def test_expired_clarification_text_is_pruned_without_an_update_candidate(db, days, eligible):
    value = {
        "created_at": (NOW - timedelta(days=days)).isoformat(),
        "text": "synthetic sensitive clarification",
        "messages": ["synthetic private"],
    }
    db.add(AppState(key="conversation:pending", value=value))
    db.flush()
    assert prune_telegram_text(db, now=NOW)["eligible_clarifications"] == int(eligible)
    assert db.get(AppState, "conversation:pending").value == value
    prune_telegram_text(db, now=NOW, apply=True)
    assert (db.get(AppState, "conversation:pending") is None) == eligible


def test_redaction_receipt_is_random_and_jobs_are_loaded_once(db, db_engine):
    from uuid import UUID

    from sqlalchemy import event

    seed(db, identity=1)
    seed(db, identity=2)
    statements = []

    def record(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT") and "FROM jobs" in statement:
            statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", record)
    try:
        assert prune_telegram_text(db, now=NOW, apply=True)["eligible_updates"] == 2
    finally:
        event.remove(db_engine, "before_cursor_execute", record)
    assert len(statements) == 1
    receipts = [db.get(TelegramUpdate, i).payload for i in (1, 2)]
    assert all("sha256" not in value and UUID(value["receipt"]).version == 4 for value in receipts)
    assert receipts[0]["receipt"] != receipts[1]["receipt"]


def test_retention_cursor_uses_age_index(db):
    from sqlalchemy import text

    db.execute(text("SET LOCAL enable_seqscan = off"))
    # Verify the ordered access path exists, independent of tiny fixture cost estimates.
    db.execute(text("SET LOCAL enable_bitmapscan = off"))
    plan = (
        db.execute(
            text(
                "EXPLAIN SELECT id FROM telegram_updates WHERE status IN ('processed', 'invalid') AND (payload ->> '_text_redacted') IS DISTINCT FROM 'true' AND received_at < now() ORDER BY received_at, id LIMIT 1000"
            )
        )
        .scalars()
        .all()
    )
    assert "ix_telegram_retention_age" in " ".join(plan)
    assert "Sort" not in " ".join(plan)


@pytest.mark.parametrize("guard", ["eligible", "missing_reply", "active_job", "recent_job"])
def test_terminal_invalid_update_respects_reply_and_job_guards(db, guard):
    payload = seed(
        db,
        status="pending" if guard == "active_job" else "done",
        completed=NOW if guard == "recent_job" else None,
    )
    update = db.get(TelegramUpdate, 1)
    update.status = "invalid"
    if guard == "missing_reply":
        db.delete(db.get(AppState, "telegram:reply:1"))
    db.flush()
    result = prune_telegram_text(db, now=NOW, apply=True)
    assert result["eligible_updates"] == int(guard == "eligible")
    if guard == "eligible":
        assert update.payload["_text_redacted"] is True
        assert db.get(AppState, "telegram:reply:1").value["text"] == REDACTED_REPLY
    else:
        assert update.payload == payload
