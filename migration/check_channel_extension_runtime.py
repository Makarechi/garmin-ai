"""Check selected out-of-process tracker reminders using the actual native worker."""

import argparse
import json
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from capture_reference import isolated_engine
from check_tracker_checkins import add_tracker, seed
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.models import Conversation

INSTANCE = "channel:synthetic:diary"
SECRET = "GA_PLUGIN_NATIVE_SYNTHETIC_CHANNEL_KEY"
METHODS = ["describe", "delivery_policy", "deliver"]


def select_channel(engine, provider, instance):
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE app_state SET value=jsonb_set(value,'{channel}',CAST(:channel AS jsonb))"
                " WHERE key='preferences:onboarding'"
            ),
            {"channel": json.dumps({"channel": provider, "instance_id": instance})},
        )


def make_due(engine):
    clock = (datetime.now(UTC) - timedelta(minutes=3)).strftime("%H:%M")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE tracker_configs SET reminder_time=:clock,reminder_timezone='UTC'"),
            {"clock": clock},
        )


@contextmanager
def worker(binary, adapter, root, engine, *, registered=True):
    integration = {
        "id": INSTANCE,
        "kind": "channel",
        "provider": "synthetic",
        "config": {"invocation_log": str(root / "invocations")},
        "secret_refs": {"key": SECRET},
    }
    env = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_DATA_DIR": str(root / "data"),
        "GA_TIMEZONE": "UTC",
        "GA_QUIET_START_HOUR": "0",
        "GA_QUIET_END_HOUR": "0",
        "GA_QUESTION_BUDGET": "4",
        "GA_INTEGRATIONS": json.dumps([integration]),
        SECRET: "synthetic-native-channel-secret",
    }
    if registered:
        env["GA_CHANNEL_PLUGIN_EXECUTABLES"] = json.dumps({INSTANCE: str(adapter)})
    with subprocess.Popen(
        [binary, "worker"], cwd=root, env=env, stdout=subprocess.PIPE, text=True
    ) as process:
        try:
            assert process.stdout.readline() == "Garmin AI worker ready\n"
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                assert process.wait(timeout=5) == 0
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                raise


def states(engine):
    with engine.connect() as connection:
        return {
            row.text: (row.state, row.attempts)
            for row in connection.execute(
                text(
                    "SELECT intent#>>'{blocks,0,text}' AS text,state,attempts FROM outbox_messages"
                )
            )
        }


def wait_for(engine, wanted, process):
    timeout = time.monotonic() + 8
    while time.monotonic() < timeout and process.poll() is None:
        existing = states(engine)
        if existing == wanted:
            return
        time.sleep(0.05)
    raise AssertionError(states(engine))


def verify(binary, adapter, root, engine):
    (root / "data" / "raw").mkdir(mode=0o700, parents=True)
    owner = seed(engine)
    with Session(engine) as session, session.begin():
        session.add(
            Conversation(
                id=uuid4(),
                owner_id=owner,
                channel="synthetic",
                channel_instance_id="diary",
                external_conversation_id="synthetic-local-recipient",
                memory_epoch=uuid4(),
                state={},
            )
        )
    make_due(engine)
    log = root / "invocations"
    select_channel(engine, "synthetic", "diary")
    with worker(binary, adapter, root, engine, registered=False):
        time.sleep(0.5)
        assert states(engine) == {} and not log.exists()

    select_channel(engine, "web", "local")
    with worker(binary, adapter, root, engine):
        time.sleep(0.5)
        assert states(engine) == {} and not log.exists()

    select_channel(engine, "synthetic", "diary")
    first = "Напоминание: Синтетическая запись private_fixture."
    second = "Напоминание: Синтетическая запись private_uncertain."
    accepted = {first: ("provider_accepted", 1)}
    with worker(binary, adapter, root, engine) as process:
        wait_for(engine, accepted, process)
    assert log.read_text().splitlines() == METHODS
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT state,provider_reference,id=outbox_message_id FROM message_delivery_receipts"
            )
        ).one() == ("provider_accepted", "local-synthetic-receipt", True)

    with Session(engine) as session, session.begin():
        add_tracker(session, "private_uncertain")
    make_due(engine)
    final = {**accepted, second: ("uncertain", 1)}
    with worker(binary, adapter, root, engine) as process:
        wait_for(engine, final, process)
    assert log.read_text().splitlines() == METHODS * 2
    with worker(binary, adapter, root, engine):
        time.sleep(0.5)
        assert states(engine) == final
    assert log.read_text().splitlines() == METHODS * 2
    print(
        "Verified explicit channel registration and selection, an accepted receipt, and no resend after an unknown outcome"
    )


def main():
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


if __name__ == "__main__":
    main()
