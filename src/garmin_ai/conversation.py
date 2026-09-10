"""Bounded analytic conversation context, never an authoritative diary fact store."""

import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from garmin_ai.events import lock_writes
from garmin_ai.models import AppState
from garmin_ai.normalize import upsert

KEY = "analysis:conversation"
MAX_BYTES = 12000


def recent_turns(value, now):
    return [
        turn
        for turn in value.get("turns", [])[-6:]
        if now - timedelta(days=7) <= datetime.fromisoformat(turn["asked_at"]) <= now
    ]


def prune_conversation(session, now):
    row = session.get(AppState, KEY, populate_existing=True)
    if row and recent_turns(row.value, now) != row.value.get("turns", []):
        lock_writes(session)
        row = session.get(AppState, KEY, populate_existing=True)
        row.value = {**row.value, "turns": recent_turns(row.value, now)}
        session.flush()


def conversation_context(session, now, reply_to_message_id=None):
    prune_conversation(session, now)
    row = session.get(AppState, KEY, populate_existing=True)
    value = row.value if row else {}
    turns = recent_turns(value, now)
    if turns:
        sent = session.scalars(
            select(AppState.key).where(
                AppState.value["status"].astext == "sent",
                or_(
                    *(
                        AppState.key.startswith(f"outbox:update:{turn['update_id']}:")
                        for turn in turns
                    )
                ),
            )
        ).all()
        delivered = {key.split(":")[2] for key in sent}
        turns = [turn for turn in turns if turn["update_id"] in delivered]
    selected = None
    if reply_to_message_id is not None:
        replies = session.scalars(
            select(AppState)
            .where(
                AppState.key.startswith("outbox:update:"),
                AppState.value["status"].astext == "sent",
                AppState.value["message_id"].as_integer() == reply_to_message_id,
            )
            .limit(2)
        ).all()
        if len(replies) == 1:
            update_id = replies[0].key.split(":")[2]
            selected = next((turn for turn in turns if turn["update_id"] == update_id), None)
        turns = [selected] if selected else []
    return {
        "epoch": value.get("epoch"),
        "turns": turns,
        "explicit_reply": reply_to_message_id is not None,
        "selection_missing": reply_to_message_id is not None and selected is None,
        "authority": "Conversation only; re-query tools for evidence. Prior answers are not facts.",
    }


def remember_answer(session, now, update_id, question, answer, evidence, *, epoch):
    if update_id is None:
        return
    lock_writes(session)
    row = session.get(AppState, KEY, populate_existing=True)
    value = row.value if row else {}
    if value.get("epoch") != epoch:
        return  # A concurrent explicit forget must not be undone by an in-flight answer.
    turns = [turn for turn in recent_turns(value, now) if turn["update_id"] != str(update_id)]
    specs = []
    for item in evidence:
        if "error" in item["result"]:
            continue
        arguments = item.get("arguments", {})
        specs.append(
            {
                "tool": item["tool"],
                "arguments": arguments
                if len(json.dumps(arguments).encode("utf-8")) <= 1000
                else None,
                "result_hash": hashlib.sha256(
                    json.dumps(item["result"], sort_keys=True).encode("utf-8")
                ).hexdigest(),
            }
        )
    turn = {
        "update_id": str(update_id),
        "asked_at": now.isoformat(),
        "question": question[:1000],
        "answer": answer[:1500],
        "specs": specs,
        "specs_truncated": any(spec["arguments"] is None for spec in specs),
        "text_truncated": len(question) > 1000 or len(answer) > 1500,
    }
    while len(json.dumps(turn, ensure_ascii=False).encode("utf-8")) > MAX_BYTES - 2 and specs:
        specs.pop()
        turn["specs_truncated"] = True
    turns = [*turns, turn][-6:]
    while (
        turns
        and len(json.dumps({"epoch": epoch, "turns": turns}, ensure_ascii=False).encode("utf-8"))
        > MAX_BYTES
    ):
        turns.pop(0)
    upsert(session, AppState, {"key": KEY, "value": {"epoch": epoch, "turns": turns}}, ["key"])


def forget_conversation(session):
    lock_writes(session)
    upsert(session, AppState, {"key": KEY, "value": {"epoch": str(uuid4()), "turns": []}}, ["key"])


def conversation_summary(session, now):
    turns = conversation_context(session, now)["turns"]
    return (
        "Контекст анализа пуст."
        if not turns
        else "Недавние вопросы для продолжения анализа:\n"
        + "\n".join(f"{turn['asked_at']}: {turn['question'][:200]}" for turn in turns)
    )
