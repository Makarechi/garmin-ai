"""Seed legacy Telegram ownership and verify Python can replay native deliveries."""

import argparse
import os

from capture_reference import isolated_engine
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.channels import ChannelInstanceRef
from garmin_ai.models import InboundMessage, Job, TelegramUpdate
from garmin_ai.telegram import save_update


def update(update_id):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "date": 1794916800,
            "text": "synthetic native inbox test",
        },
    }


def seed_legacy(session):
    assert save_update(session, update(111), 42, dispatcher_version="legacy-v1")
    assert save_update(
        session,
        update(111),
        42,
        dispatcher_version="legacy-v1",
        channel_instance=ChannelInstanceRef(channel="telegram", instance_id="secondary"),
    )
    assert session.scalar(select(func.count()).select_from(InboundMessage)) == 0
    assert (
        session.scalar(
            select(func.count()).select_from(TelegramUpdate).where(TelegramUpdate.id < 0)
        )
        == 1
    )


def verify_replays(session):
    native = session.scalar(
        select(InboundMessage).where(
            InboundMessage.external_event_id == "112",
            InboundMessage.channel_instance_id == "primary",
        )
    )
    assert native is not None and native.legacy_telegram_update_id is None
    assert (
        session.scalar(select(Job).where(Job.dedup_key == f"telegram:inbound:native:{native.id}"))
        is not None
    )
    count = session.scalar(select(func.count()).select_from(Job))
    for version in ("legacy-v1", "neutral-shadow-v1"):
        assert save_update(session, update(112), 42, dispatcher_version=version)
        assert session.get(TelegramUpdate, 112) is None
        assert session.scalar(select(func.count()).select_from(Job)) == count
    assert native.legacy_telegram_update_id is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=["seed", "verify"])
    args = parser.parse_args()
    url = make_url(os.environ["GA_TEST_DATABASE_URL"])
    if (
        url.get_backend_name() != "postgresql"
        or not (url.database or "").endswith("_test")
        or url.query
    ):
        raise ValueError("Expected a dedicated PostgreSQL fixture database")
    engine = isolated_engine() if args.step == "seed" else create_engine(url, hide_parameters=True)
    try:
        with Session(engine) as session, session.begin():
            assert session.scalar(text("SELECT current_database()")) == url.database
            if args.step == "seed":
                seed_legacy(session)
            else:
                verify_replays(session)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
