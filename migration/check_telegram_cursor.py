"""Record the pinned Telegram ordering and polling cursor with a local fake bot."""

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.models import AppState, TelegramUpdate
from garmin_ai.telegram import poll, save_update

BASE = datetime.fromisoformat("2026-11-12T10:00:00+00:00")
WEEK = timedelta(days=7)
OWNER = 1234567890123


def at(instant):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    return Clock


def wire(update_id, owner):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": owner},
            "chat": {"id": owner, "type": "private"},
            "text": "synthetic cursor event",
        },
    }


async def probe(engine, instance, foreign):
    stop = asyncio.Event()
    offsets = []

    class Bot:
        async def get_updates(self, *, offset, **kwargs):
            offsets.append(offset)
            stop.set()
            if foreign is None:
                return []
            return [SimpleNamespace(update_id=foreign, to_dict=lambda: wire(foreign, OWNER + 1))]

    settings = SimpleNamespace(telegram_user_id=OWNER, telegram_dispatcher_version="legacy-v1")
    await poll(Bot(), engine, settings, stop, channel_instance=instance)
    assert len(offsets) == 1
    return offsets[0]


def snapshot(session):
    return dict(
        session.execute(
            select(AppState.key, AppState.value).where(
                AppState.key.startswith("telegram:offset")
                | AppState.key.startswith("telegram:ordering")
            )
        ).all()
    )


def capture(output):
    # Each operation captures the whole relevant saved state so silent cursor or epoch
    # changes on another bot are visible in the native comparison.
    calls = [
        ("read", "primary", BASE, None),
        ("seed", "telegram:offset", BASE, {"offset": 12}),
        ("read", "primary", BASE, None),
        (
            "seed",
            "telegram:offset",
            BASE,
            {"offset": 15, "received_at": (BASE - WEEK + timedelta(microseconds=1)).isoformat()},
        ),
        ("read", "primary", BASE, None),
        ("read", "primary", BASE + timedelta(microseconds=1), None),
        ("seed", "telegram:ordering", BASE, {"epoch": 3, "last_received_at": BASE.isoformat()}),
        ("seed", "telegram:offset", BASE, {"offset": 31}),
        ("read", "primary", BASE + timedelta(hours=1), None),
        ("poll_foreign", "secondary", BASE, 8),
        ("read", "primary", BASE + timedelta(hours=2), None),
        ("read", "secondary", BASE + timedelta(hours=2), None),
        ("order", "primary", BASE + WEEK, 200),
        ("order", "primary", BASE + WEEK + timedelta(seconds=1), 201),
        ("order", "secondary", BASE + WEEK + timedelta(seconds=2), 202),
        ("order", "secondary", BASE + 2 * WEEK + timedelta(seconds=2), 203),
        ("poll_foreign", "primary", BASE + 2 * WEEK, 9),
        ("read", "primary", BASE + 2 * WEEK + timedelta(minutes=1), None),
    ]
    engine = isolated_engine()
    try:
        results = []
        for kind, instance, now, arg in calls:
            outcome = None
            with patch("garmin_ai.telegram.datetime", at(now)):
                if kind == "seed":
                    with Session(engine) as session, session.begin():
                        session.merge(AppState(key=instance, value=arg))
                elif kind == "order":
                    with Session(engine) as session, session.begin():
                        assert save_update(
                            session,
                            wire(arg, OWNER),
                            OWNER,
                            dispatcher_version="legacy-v1",
                            channel_instance=ChannelInstanceRef(
                                channel="telegram", instance_id=instance
                            ),
                        )
                        key = (
                            "telegram:ordering"
                            if instance == "primary"
                            else f"telegram:ordering:telegram:{instance}"
                        )
                        outcome = session.get(AppState, key).value["epoch"]
                else:
                    outcome = asyncio.run(
                        probe(
                            engine,
                            ChannelInstanceRef(channel="telegram", instance_id=instance),
                            arg,
                        )
                    )
            with Session(engine) as session:
                results.append(
                    {
                        "kind": kind,
                        "instance": instance,
                        "now": now.isoformat(),
                        "arg": arg,
                        "outcome": outcome,
                        "state": snapshot(session),
                    }
                )
        # Old databases sometimes have persisted updates without an ordering marker.
        with Session(engine) as session, session.begin():
            row = session.get(AppState, "telegram:ordering")
            session.delete(row)
            session.add(
                TelegramUpdate(id=301, payload={"update_id": 301}, received_at=BASE + 3 * WEEK)
            )
        now = BASE + 4 * WEEK
        with (
            patch("garmin_ai.telegram.datetime", at(now)),
            Session(engine) as session,
            session.begin(),
        ):
            assert save_update(session, wire(302, OWNER), OWNER, dispatcher_version="legacy-v1")
            results.append(
                {
                    "kind": "legacy_order",
                    "instance": "primary",
                    "now": now.isoformat(),
                    "arg": {"received_at": (BASE + 3 * WEEK).isoformat()},
                    "outcome": 1,
                    "state": snapshot(session),
                }
            )
        output.write_text(json.dumps(results), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
