"""Compare scheduled tracker check-ins with the pinned Python implementation."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from capture_reference import isolated_engine
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner
from garmin_ai.config import Settings
from garmin_ai.initiative_rules import queue_due_tracker_checkins
from garmin_ai.models import AppState, ChannelBinding, Conversation, OutboxMessage
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

CHAT = 918724
NOW = datetime(2026, 10, 25, 21, 50, tzinfo=UTC)
SETTINGS = Settings(quiet_start_hour=22, quiet_end_hour=6, question_budget=3)


def add_tracker(session, key, *, clock="20:00", privacy="private"):
    draft = TrackerSetupDraft(
        key=key,
        name="Synthetic tracker",
        locale="ru",
        fields=[
            TrackerFieldDraft(key="value", label="Count", kind="integer", minimum=1, maximum=20)
        ],
        shortcut="Синтетическая запись " + key,
        reminder_enabled=True,
        reminder_time=clock,
        reminder_timezone="Europe/Bratislava",
        privacy=privacy,
    )
    preview = preview_tracker(session, draft)
    return confirm_tracker(
        session,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="synthetic",
    )


def seed(engine):
    with Session(engine) as session, session.begin():
        person = owner(session)
        person.locale = "ru"
        session.add(
            ChannelBinding(
                owner_id=person.id,
                channel="telegram",
                channel_instance_id="primary",
                external_id=str(CHAT),
                confirmation_method="synthetic",
            )
        )
        for channel, instance in [
            ("local-example", "fallback"),
            ("duplicate-example", "ambiguous"),
            ("duplicate-example", "ambiguous"),
        ]:
            session.add(
                Conversation(
                    id=uuid4(),
                    owner_id=person.id,
                    channel=channel,
                    channel_instance_id=instance,
                    memory_epoch=uuid4(),
                    state={},
                )
            )
        session.merge(
            AppState(
                key="preferences:onboarding",
                value={
                    "channel": {"channel": "telegram", "instance_id": "primary"},
                    "fallback_channels": [
                        {"channel": "local-example", "instance_id": "fallback"},
                        {"channel": "duplicate-example", "instance_id": "ambiguous"},
                        {"channel": "missing-example", "instance_id": "unavailable"},
                    ],
                },
            )
        )
        add_tracker(session, "private_fixture")
        add_tracker(session, "sensitive_fixture", privacy="sensitive")
        owner_id = person.id
    return owner_id


def reference(engine):
    with Session(engine) as session:
        rows = queue_due_tracker_checkins(session, SETTINGS, NOW)
        assert len(rows) == 1
        rows = session.scalars(select(OutboxMessage).order_by(OutboxMessage.dedup_key)).all()
        return {
            "rules": {
                row.key: row.value
                for row in session.scalars(
                    select(AppState).where(AppState.key.startswith("initiative:rule:"))
                )
            },
            "outbox": [
                {
                    "key": row.dedup_key,
                    "state": row.state,
                    "intent": {
                        key: value for key, value in row.intent.items() if key != "intent_id"
                    },
                    "retry": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
                }
                for row in rows
            ],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        owner_id = seed(engine)
        if args.worker:
            due = (datetime.now(UTC) - timedelta(minutes=3)).strftime("%H:%M")
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE tracker_configs SET reminder_time=:clock,reminder_timezone='UTC'"),
                    {"clock": due},
                )
            subprocess.run(
                [
                    args.binary.resolve(strict=True),
                    "--ignored",
                    "--exact",
                    "telegram_transport::tests::reminders::tracker_worker_retries_refusal_and_holds_uncertain",
                    "--nocapture",
                ],
                env={
                    **os.environ,
                    "GA_TEST_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                },
                check=True,
            )
            return
        document = {"owner": str(owner_id), "now": NOW.isoformat(), "chat": CHAT} | reference(
            engine
        )
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "tracker-checkins.json"
            fixture.write_text(json.dumps(document), encoding="utf-8")
            subprocess.run(
                [args.binary.resolve(strict=True), "--ignored", "--test-threads=1", "--nocapture"],
                env={
                    **os.environ,
                    "GA_TEST_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                    "GA_TEST_TRACKER_CHECKINS": str(fixture),
                },
                check=True,
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
