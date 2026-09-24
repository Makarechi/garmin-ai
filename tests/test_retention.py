from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import BigInteger, cast, func, select

from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import (
    AppState,
    Audit,
    Event,
    Job,
    OutboxMessage,
    TelegramUpdate,
)
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


def test_redacted_secondary_update_keeps_its_transport_identity(db):
    seed(db, identity=77)
    secondary = ChannelInstanceRef(channel="telegram", instance_id="secondary")
    payload = {
        "update_id": 77,
        "message": {
            "message_id": 77,
            "date": int((NOW - timedelta(days=100)).timestamp()),
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42},
            "text": "/status",
        },
    }
    assert save_update(db, payload, 42, channel_instance=secondary)
    secondary_row = db.scalar(select(TelegramUpdate).where(TelegramUpdate.id < 0))
    secondary_row.status = "processed"
    secondary_row.received_at = NOW - timedelta(days=100)
    job = db.scalar(
        select(Job).where(cast(Job.payload["update_id"].astext, BigInteger) == secondary_row.id)
    )
    job.status = "done"
    job.completed_at = NOW - timedelta(days=100)
    db.add(AppState(key=f"telegram:reply:{secondary_row.id}", value={"text": "synthetic"}))
    db.flush()

    prune_telegram_text(db, now=NOW, apply=True)

    assert secondary_row.payload["update_id"] == 77
    assert secondary_row.payload["_channel_instance"] == secondary.model_dump()
    before = db.scalar(select(func.count()).select_from(TelegramUpdate))
    assert save_update(db, payload, 42, channel_instance=secondary)
    assert db.scalar(select(func.count()).select_from(TelegramUpdate)) == before


@pytest.mark.parametrize("terminal_state", ["provider_accepted", "failed"])
def test_neutral_message_text_is_pruned_only_after_terminal_delivery(db, terminal_state):
    from garmin_ai.accounts import owner
    from garmin_ai.channels import ChannelInstanceRef, InboundEnvelope, InboundKind
    from garmin_ai.dialogue import ingest_envelope

    person = owner(db)
    message, _ = ingest_envelope(
        db,
        InboundEnvelope(
            owner_id=person.id,
            channel_instance=ChannelInstanceRef(channel="test", instance_id="restricted"),
            conversation_id=uuid4(),
            external_event_id="old-event",
            external_message_id="old-message",
            sender_ref="owner",
            occurred_at=NOW - timedelta(days=100),
            received_at=NOW - timedelta(days=100),
            kind=InboundKind.TEXT,
            text="synthetic private neutral text",
        ),
    )
    message.status = "processed"
    outbox = OutboxMessage(
        owner_id=person.id,
        conversation_id=message.conversation_id,
        inbound_message_id=message.id,
        operation_id=message.operation_id,
        intent={"text": "synthetic private neutral reply"},
        dedup_key="neutral-old",
        state="queued",
    )
    db.add(outbox)
    db.flush()

    assert prune_telegram_text(db, now=NOW, apply=True)["eligible_neutral_messages"] == 0
    assert "private" in message.normalized_text
    outbox.state = terminal_state
    db.flush()
    assert prune_telegram_text(db, now=NOW, apply=True)["eligible_neutral_messages"] == 1
    assert message.normalized_text is None
    assert message.envelope["_text_redacted"] is True
    assert outbox.intent["_text_redacted"] is True


def test_terminal_outbox_without_inbound_link_is_pruned(db):
    from garmin_ai.accounts import owner
    from garmin_ai.models import Conversation

    person = owner(db)
    conversation = Conversation(
        owner_id=person.id,
        channel="test",
        channel_instance_id="restricted",
        external_conversation_id="orphan-retention",
    )
    db.add(conversation)
    db.flush()
    outbox = OutboxMessage(
        owner_id=person.id,
        conversation_id=conversation.id,
        inbound_message_id=None,
        operation_id=uuid4(),
        intent={"text": "synthetic private asynchronous reply"},
        dedup_key="neutral-orphan-old",
        state="failed",
        created_at=NOW - timedelta(days=100),
    )
    db.add(outbox)
    db.flush()

    result = prune_telegram_text(db, now=NOW, apply=True)

    assert result["eligible_neutral_messages"] == 1
    assert outbox.intent["_text_redacted"] is True


def test_orphaned_outbox_cursor_reaches_every_page_without_applying(db):
    from garmin_ai.accounts import owner
    from garmin_ai.models import Conversation

    person = owner(db)
    conversation = Conversation(
        owner_id=person.id,
        channel="test",
        channel_instance_id="restricted",
        external_conversation_id="orphan-pages",
    )
    db.add(conversation)
    db.flush()
    for index in range(3):
        db.add(
            OutboxMessage(
                owner_id=person.id,
                conversation_id=conversation.id,
                operation_id=uuid4(),
                intent={"text": f"synthetic private orphan {index}"},
                dedup_key=f"neutral-orphan-page-{index}",
                state="failed",
                created_at=NOW - timedelta(days=100, minutes=3 - index),
            )
        )
    db.flush()

    cursor = None
    pages = []
    while True:
        result = prune_telegram_text(
            db,
            now=NOW,
            limit=1,
            apply=False,
            neutral_cursor=cursor,
        )
        pages.append(result["scanned_neutral_messages"])
        cursor = result["next_neutral_cursor"]
        if cursor is None:
            break

    assert pages == [1, 1, 1]


def test_terminal_orphan_outboxes_expose_a_retention_cursor(db):
    from garmin_ai.accounts import owner
    from garmin_ai.models import Conversation

    person = owner(db)
    conversation = Conversation(
        owner_id=person.id,
        channel="test",
        channel_instance_id="restricted",
        external_conversation_id="orphan-retention-page",
    )
    db.add(conversation)
    db.flush()
    rows = []
    for index in range(2):
        row = OutboxMessage(
            owner_id=person.id,
            conversation_id=conversation.id,
            inbound_message_id=None,
            operation_id=uuid4(),
            intent={"text": f"synthetic private asynchronous reply {index}"},
            dedup_key=f"neutral-orphan-page-{index}",
            state="failed",
            created_at=NOW - timedelta(days=100) + timedelta(seconds=index),
        )
        db.add(row)
        rows.append(row)
    db.flush()

    first = prune_telegram_text(db, now=NOW, limit=1, apply=True)
    assert first["eligible_neutral_messages"] == 1
    assert first["next_neutral_cursor"]
    second = prune_telegram_text(
        db,
        now=NOW,
        limit=1,
        apply=True,
        neutral_cursor=first["next_neutral_cursor"],
    )
    assert second["eligible_neutral_messages"] == 1
    assert all(row.intent["_text_redacted"] is True for row in rows)


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
    neutral_cursor = json.dumps(["1970-01-01T00:00:00+00:00", str(uuid4())])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "garmin-ai",
            "prune-telegram-text",
            "--apply",
            "--neutral-cursor",
            neutral_cursor,
        ],
    )
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


@pytest.mark.parametrize(
    "stamp,eligible",
    [
        ((NOW - timedelta(days=100)).isoformat(), True),
        ((NOW - timedelta(days=10)).isoformat(), False),
        (None, False),
        ("invalid", False),
    ],
)
def test_proactive_answer_text_obeys_retention_without_erasing_history(db, stamp, eligible):
    from garmin_ai.models import PendingQuestion

    question = PendingQuestion(
        kind="migraine",
        status="acknowledged",
        text="synthetic question",
        evidence={"answer_text": "synthetic answer", "answered_at": stamp, "synthetic": True},
        priority=1,
        earliest_send_at=NOW - timedelta(days=120),
        expires_at=NOW - timedelta(days=110),
        dedup_key="synthetic-retention-answer",
    )
    db.add(question)
    db.flush()
    identity = question.id
    preview = prune_telegram_text(db, now=NOW)
    assert preview["eligible_proactive_answers"] == int(eligible)
    assert question.evidence["answer_text"] == "synthetic answer"
    result = prune_telegram_text(db, now=NOW, apply=True)
    assert result["eligible_proactive_answers"] == int(eligible)
    db.refresh(question)
    assert ("answer_text" not in question.evidence) == eligible
    assert question.id == identity and question.status == "acknowledged"
    assert question.evidence["answered_at"] == stamp
    assert question.evidence["synthetic"] is True
    assert prune_telegram_text(db, now=NOW, apply=True)["eligible_proactive_answers"] == 0


@pytest.mark.parametrize("apply", [False, True])
def test_answer_retention_pages_past_recent_and_invalid_timestamps(db, apply):
    from uuid import UUID

    from garmin_ai.models import PendingQuestion

    stamps = [NOW.isoformat(), "invalid", (NOW - timedelta(days=100)).isoformat()]
    for index, stamp in enumerate(stamps, 1):
        db.add(
            PendingQuestion(
                id=UUID(int=index),
                kind="migraine",
                status="acknowledged",
                text="synthetic",
                evidence={"answer_text": "synthetic", "answered_at": stamp},
                priority=1,
                earliest_send_at=NOW,
                expires_at=NOW + timedelta(days=1),
                dedup_key=f"synthetic-answer-{index}",
            )
        )
    db.flush()
    cursor = None
    for expected in (0, 0, 1):
        result = prune_telegram_text(db, now=NOW, limit=1, apply=apply, answer_cursor=cursor)
        assert result["scanned_proactive_answers"] == 1
        assert result["eligible_proactive_answers"] == expected
        assert result["next_answer_cursor"] != cursor
        cursor = result["next_answer_cursor"]
    assert (
        prune_telegram_text(db, now=NOW, limit=1, answer_cursor=cursor)["next_answer_cursor"]
        is None
    )
    remaining = list(
        db.scalars(
            select(PendingQuestion).where(
                PendingQuestion.evidence["answer_text"].astext.is_not(None)
            )
        )
    )
    assert len(remaining) == (2 if apply else 3)


def test_answer_retention_has_ordered_partial_index(db):
    from sqlalchemy import text

    db.execute(text("SET LOCAL enable_seqscan = off"))
    db.execute(text("SET LOCAL enable_bitmapscan = off"))
    plan = (
        db.execute(
            text(
                "EXPLAIN SELECT id FROM pending_questions WHERE (evidence ->> 'answer_text') IS NOT NULL ORDER BY id LIMIT 1"
            )
        )
        .scalars()
        .all()
    )
    assert "ix_question_answer_retention" in " ".join(plan)
    assert "Sort" not in " ".join(plan)


@pytest.mark.parametrize(
    "age,retained", [(timedelta(days=100), False), (timedelta(minutes=30), True)]
)
def test_new_clarification_does_not_renew_stale_history(db, age, retained):
    from garmin_ai.agent import Interpretation, apply_command

    db.add(
        AppState(
            key="conversation:pending",
            value={
                "created_at": (NOW - age).isoformat(),
                "text": "synthetic old text",
                "messages": [{"text": "synthetic old text", "question": "old?"}],
            },
        )
    )
    db.flush()
    apply_command(
        db,
        Interpretation(intent="clarify", confidence=1, clarification="new?"),
        text="synthetic new text",
        update_id=909,
        actor="owner",
        now=NOW,
    )
    row = db.get(AppState, "conversation:pending", populate_existing=True)
    assert (
        any(message["text"] == "synthetic old text" for message in row.value["messages"])
        == retained
    )
    assert row.value["text"] == "synthetic new text"


def test_answer_index_is_registered_for_autogeneration():
    from garmin_ai.models import PendingQuestion

    index = next(
        index
        for index in PendingQuestion.__table__.indexes
        if index.name == "ix_question_answer_retention"
    )
    assert [column.name for column in index.columns] == ["id"]
    assert index.dialect_options["postgresql"]["where"] is not None
