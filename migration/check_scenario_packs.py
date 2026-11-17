"""Compare native pack defaults, revisions and privacy effects against pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner
from garmin_ai.models import AppState, Conversation, PendingQuestion
from garmin_ai.scenario_packs import configure_scenario_pack, list_scenario_packs

INITIAL_EPOCH = "528aac9d-dc33-4c65-ad92-363ff41ed41f"
ENABLED = {
    "revision": 1,
    "tracking_enabled": True,
    "collection_enabled": False,
    "reminders_enabled": False,
    "visible": True,
    "llm_enabled": True,
    "outcome_goal": "Синтетическая цель",
}
DISABLED = {
    **ENABLED,
    "revision": 2,
    "tracking_enabled": False,
    "llm_enabled": False,
    "reminders_enabled": True,
}


def seed(engine, legacy):
    with Session(engine) as session, session.begin():
        person = owner(session)
        conversation = Conversation(
            owner_id=person.id,
            channel="telegram",
            channel_instance_id="telegram:primary",
            memory_epoch=UUID(INITIAL_EPOCH),
            state={"remembered": "Synthetic analysis"},
        )
        session.add(conversation)
        states = {
            "analysis:conversation": {"epoch": INITIAL_EPOCH, "turns": [{"update_id": 14}]},
            "analysis:conversation:pending:synthetic": {"turn": {"update_id": 15}},
            "telegram:reply:14": {"kind": "chat", "text": "Synthetic remembered reply"},
            "telegram:reply:15": {"kind": "chat", "text": "Synthetic pending reply"},
            "telegram:reply:16": {"kind": "analysis", "text": "Synthetic analysis reply"},
            "telegram:reply:17": {"kind": "chat", "text": "Unrelated synthetic reply"},
            "conversation:pending:owner": {"pack": "caffeine", "type": "synthetic"},
            "conversation:pending:unspecified": {"type": "synthetic"},
            "conversation:pending:unrelated": {"pack": "wellbeing", "type": "synthetic"},
        }
        if legacy:
            states["migration:legacy-scenario-packs"] = {"synthetic": True}
        session.add_all([AppState(key=key, value=value) for key, value in states.items()])
        base = datetime(2025, 4, 18, tzinfo=UTC)
        for kind in ["caffeine", "migraine"]:
            for status in ["sent", "uncertain", "pending", "answered"]:
                session.add(
                    PendingQuestion(
                        kind=kind,
                        text="Synthetic question",
                        evidence={},
                        priority=1.0,
                        earliest_send_at=base,
                        expires_at=base + timedelta(hours=1),
                        status=status,
                        dedup_key=f"synthetic:{kind}:{status}",
                    )
                )


def write_python(engine):
    with Session(engine) as session, session.begin():
        initial = list_scenario_packs(session)
        enabled = configure_scenario_pack(session, "caffeine", ENABLED)
        while_enabled = list_scenario_packs(session)
        disabled = configure_scenario_pack(session, "caffeine", DISABLED)
        return {
            "initial": initial,
            "enabled": enabled,
            "while_enabled": while_enabled,
            "disabled": disabled,
        }


def snapshot(engine):
    with Session(engine) as session:
        states = {row.key: row.value for row in session.scalars(select(AppState))}
        context = states["analysis:conversation"]
        assert UUID(context.pop("epoch")) != UUID(INITIAL_EPOCH)
        conversation = session.scalar(select(Conversation))
        assert conversation.memory_epoch != UUID(INITIAL_EPOCH)
        reminders = {row.dedup_key: row.status for row in session.scalars(select(PendingQuestion))}
        return states, conversation.state, reminders


def run(binary, legacy):
    source = os.environ["GA_TEST_DATABASE_URL"]
    rust_target = os.environ["GA_TEST_RUST_DATABASE_URL"]
    if rust_target == source:
        raise ValueError("Use two distinct disposable databases")
    python = isolated_engine()
    try:
        seed(python, legacy)
        expected = write_python(python)
        os.environ["GA_TEST_DATABASE_URL"] = rust_target
        rust = isolated_engine()
        try:
            seed(rust, legacy)
            subprocess.run(
                [binary, "--ignored"],
                env={**os.environ, "GA_TEST_SCENARIO_EXPECTED": json.dumps(expected)},
                check=True,
            )
            assert snapshot(rust) == snapshot(python)
            print("Scenario preferences, privacy effects and reminder states match Python")
        finally:
            rust.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = source
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--legacy", action="store_true")
    arguments = parser.parse_args()
    run(arguments.binary.resolve(strict=True), arguments.legacy)
