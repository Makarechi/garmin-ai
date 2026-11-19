"""Compare restart-safe native channel fallback with the pinned Python rule."""

import argparse
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from capture_reference import isolated_engine
from check_channel_extension_runtime import METHODS, SECRET, make_due, worker
from check_tracker_checkins import add_tracker, seed
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.initiative_rules import reroute_failed
from garmin_ai.models import AppState, Conversation, OutboxMessage

PRIMARY = {"channel": "synthetic", "instance_id": "diary"}
RESERVE = {"channel": "synthetic", "instance_id": "reserve"}


def integration(root, route, reject=None):
    instance = route["instance_id"]
    return {
        "id": f"channel:synthetic:{instance}",
        "kind": "channel",
        "provider": "synthetic",
        "config": {
            "invocation_log": str(root / instance),
            "reject_text": reject,
        },
        "secret_refs": {"key": SECRET},
    }


def deliveries(engine, label):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT id,operation_id,conversation_id,dedup_key,intent,state,attempts"
                " FROM outbox_messages WHERE intent#>>'{blocks,0,text}' LIKE :label"
                " ORDER BY dedup_key"
            ),
            {"label": f"% {label}."},
        ).all()


def pending(engine, id):
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT count(*) FROM app_state WHERE starts_with(key,'initiative:fallback:')"
                " AND right(key,36)=:id"
            ),
            {"id": str(id)},
        )


def wait_for(engine, label, wanted, process):
    deadline = time.monotonic() + 12
    rows = ()
    while time.monotonic() < deadline and process.poll() is None:
        rows = deliveries(engine, label)
        if [row.state for row in rows] == wanted:
            return rows
        time.sleep(0.05)
    raise AssertionError(rows)


def expected_fallback(engine, id):
    with Session(engine) as session:
        original = session.scalar(select(OutboxMessage).where(OutboxMessage.id == id))
        fallback = reroute_failed(session, original, now=datetime.now(UTC))
        assert fallback is not None
        session.flush()
        return (
            fallback.conversation_id,
            fallback.operation_id,
            fallback.dedup_key,
            {name: value for name, value in fallback.intent.items() if name != "intent_id"},
        )


def verify(binary, adapter, root, engine):
    (root / "data" / "raw").mkdir(mode=0o700, parents=True)
    owner = seed(engine)
    with Session(engine) as session, session.begin():
        for route in (PRIMARY, RESERVE):
            session.add(
                Conversation(
                    id=uuid4(),
                    owner_id=owner,
                    channel=route["channel"],
                    channel_instance_id=route["instance_id"],
                    external_conversation_id=f"synthetic-{route['instance_id']}",
                    memory_epoch=uuid4(),
                    state={},
                )
            )
        session.merge(
            AppState(
                key="preferences:onboarding",
                value={
                    "channel": PRIMARY,
                    "fallback_channels": [
                        {"channel": "duplicate-example", "instance_id": "ambiguous"},
                        RESERVE,
                    ],
                },
            )
        )
    make_due(engine)

    primary = integration(root, PRIMARY, "private_fixture")
    reserve = integration(root, RESERVE)
    with worker(binary, adapter, root, engine, profile=[primary]) as process:
        (failed,) = wait_for(engine, "private_fixture", ["failed"], process)
    assert pending(engine, failed.id) == 1
    expected = expected_fallback(engine, failed.id)

    with worker(binary, adapter, root, engine, profile=[primary, reserve]) as process:
        original, copied = wait_for(
            engine, "private_fixture", ["failed", "provider_accepted"], process
        )
    actual = (
        copied.conversation_id,
        copied.operation_id,
        copied.dedup_key,
        {name: value for name, value in copied.intent.items() if name != "intent_id"},
    )
    assert actual == expected, (actual, expected)
    assert original.id == failed.id and original.id != copied.id
    assert original.attempts == copied.attempts == 1
    assert original.intent["logical_notification_id"] == copied.intent["logical_notification_id"]
    assert pending(engine, failed.id) == 0

    with Session(engine) as session, session.begin():
        add_tracker(session, "private_uncertain")
    make_due(engine)
    with worker(binary, adapter, root, engine, profile=[primary, reserve]) as process:
        (uncertain,) = wait_for(engine, "private_uncertain", ["uncertain"], process)
    assert pending(engine, uncertain.id) == 0
    with worker(binary, adapter, root, engine, profile=[primary, reserve]):
        time.sleep(0.5)
        assert deliveries(engine, "private_uncertain") == [uncertain]
        assert deliveries(engine, "private_fixture") == [original, copied]
    assert (root / "reserve").read_text().splitlines() == METHODS

    with Session(engine) as session, session.begin():
        add_tracker(session, "private_paused")
    make_due(engine)
    primary_paused = integration(root, PRIMARY, "private_paused")
    with worker(binary, adapter, root, engine, profile=[primary_paused]) as process:
        (paused,) = wait_for(engine, "private_paused", ["failed"], process)
    assert pending(engine, paused.id) == 1
    with Session(engine) as session, session.begin():
        session.merge(AppState(key="proactive:enabled", value={"enabled": False}))
    with worker(binary, adapter, root, engine, profile=[primary_paused, reserve]):
        deadline = time.monotonic() + 5
        while pending(engine, paused.id) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pending(engine, paused.id) == 0
        assert deliveries(engine, "private_paused") == [paused]
    assert (root / "reserve").read_text().splitlines() == METHODS
    assert (root / "diary").read_text().splitlines() == METHODS * 3
    print(
        "Matched Python fallback after a known failure and restart; uncertain and paused sends stayed closed"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("adapter", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            verify(
                args.binary.resolve(strict=True),
                args.adapter.resolve(strict=True),
                Path(directory),
                engine,
            )
    finally:
        engine.dispose()
