"""Exercise restore boundaries before and after native writes on disposable stores."""

import argparse
import hashlib
import json
import os
import selectors
import shutil
import subprocess
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID

import httpx
from check_backup_commands import (
    capture,
    check_recovered_files,
    check_rows,
    cli_environment,
    provision_engine,
    run_cli,
    settings,
)
from check_database_export import AT, SOURCE, table_values
from check_diary_http import NOTE, PATH, TOKENS, headers
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.models import (
    AppState,
    ChannelBinding,
    Conversation,
    HealthDay,
    Job,
    MessageDeliveryReceipt,
    OutboxMessage,
    Person,
    SourceConnection,
)
from garmin_ai.operations import create_backup


def prepare_state(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id).where(Person.singleton))
        conversation = UUID(int=870)
        reply = UUID(int=871)
        session.add_all(
            [
                SourceConnection(
                    owner_id=owner,
                    provider="garmin",
                    namespace="garmin_connect",
                    external_id="synthetic-owner",
                    confirmation_method="fixture",
                ),
                ChannelBinding(
                    owner_id=owner,
                    channel="telegram",
                    channel_instance_id="primary",
                    external_id="42",
                    confirmation_method="fixture",
                ),
                Conversation(
                    id=conversation,
                    owner_id=owner,
                    channel="telegram",
                    channel_instance_id="primary",
                    external_conversation_id="42",
                ),
                AppState(
                    key="telegram:offset", value={"offset": 77, "received_at": AT.isoformat()}
                ),
                AppState(
                    key="conversation:pending",
                    value={"created_at": AT.isoformat(), "question": "Synthetic clarification?"},
                ),
                AppState(
                    key="replay:source_scan_cursor",
                    value={"fetched_at": AT.isoformat(), "id": str(SOURCE)},
                ),
                Job(
                    kind="raw_replay",
                    payload={"raw_ref": str(SOURCE), "target_version": 9, "backfill": True},
                    dedup_key=f"raw-replay:{SOURCE}:9",
                    run_at=AT + timedelta(days=1),
                    attempts=2,
                ),
            ]
        )
        session.flush()
        session.add(
            OutboxMessage(
                id=reply,
                owner_id=owner,
                conversation_id=conversation,
                operation_id=UUID(int=872),
                intent={
                    "owner_id": str(owner),
                    "conversation_id": str(conversation),
                    "channel_instance": {"channel": "telegram", "instance_id": "primary"},
                    "blocks": [{"text": "Synthetic uncertain send"}],
                },
                dedup_key="synthetic:uncertain:reply",
                state="uncertain",
                attempts=1,
            )
        )
        session.flush()
        session.add(
            MessageDeliveryReceipt(outbox_message_id=reply, state="uncertain", observed_at=AT)
        )
        session.add(
            OutboxMessage(
                id=UUID(int=873),
                owner_id=owner,
                conversation_id=conversation,
                operation_id=UUID(int=874),
                intent={
                    "owner_id": str(owner),
                    "conversation_id": str(conversation),
                    "channel_instance": {"channel": "telegram", "instance_id": "primary"},
                    "blocks": [{"text": "Synthetic unsent outbound reply"}],
                },
                dedup_key="synthetic:queued:reply",
                state="queued",
                attempts=0,
            )
        )


def native_environment(config):
    return {
        **cli_environment(config),
        "GA_API_TOKENS": json.dumps(TOKENS),
        "GA_INTEGRATIONS": "[]",
    }


@contextmanager
def native_api(binary, root, config, *, read_only=False):
    with subprocess.Popen(
        [binary, "serve", "--port", "0", *(["--read-only"] if read_only else [])],
        cwd=root,
        env=native_environment(config),
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=5), "Candidate API did not announce its address"
            line = process.stdout.readline().strip()
            assert line.startswith("Listening on http://127.0.0.1:")
            with httpx.Client(base_url=line.split(" on ", 1)[1], timeout=5, trust_env=False) as api:
                assert api.get("/health/ready").status_code == 200
                yield api
        finally:
            process.terminate()
            process.wait(timeout=5)


def restore(binary, root, config, archive, expected, original, engine):
    started = monotonic()
    recovery = root / f"{archive.stem}-files"
    run_cli(binary, root, config, "unpack-backup", archive, recovery)
    check_recovered_files(recovery, original)
    run_cli(binary, root, config, "restore-db", recovery / "database.jsonl.gz")
    shutil.copytree(recovery / "raw", config.data_dir / "raw")
    shutil.copytree(recovery / "tokens", config.token_dir)
    shutil.copy2(recovery / "coverage-report.json", config.data_dir)
    check_rows(engine, expected)
    return round(monotonic() - started, 3)


def archive_digest(archive):
    with archive.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def previous_api(config, engine):
    tokens = Settings(_env_file=None, api_tokens=TOKENS).api_tokens
    config = config.model_copy(update={"api_tokens": tokens, "integrations": []})
    return TestClient(create_app(config, engine=engine))


def health_snapshot(api, day):
    return api.post(
        "/tools/health_snapshot",
        headers=headers("health"),
        json={"arguments": {"day": day.isoformat()}},
    )


def inspect_before_writes(binary, root, config, engine, event_path, expected, pending_health):
    lock = text("SELECT pg_try_advisory_xact_lock(72104620)")
    with engine.begin() as connection:
        assert connection.scalar(lock)
        refused = subprocess.run(
            [binary, "serve", "--port", "0", "--read-only"],
            cwd=root,
            env=native_environment(config),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert refused.returncode == 1 and not refused.stdout
        assert "Another Garmin AI worker is already running" in refused.stderr

    with native_api(binary, root, config, read_only=True) as native:
        assert native.get(event_path, headers=headers("reader")).json() == expected
        assert native.get(event_path).status_code == 401
        tools = native.get("/tools", headers=headers("reader"))
        assert tools.status_code == 200 and tools.json()
        health = health_snapshot(native, AT.date())
        assert (health.status_code, health.json()) == (503, pending_health)
        for path in (
            PATH,
            "/wearable/marks",
            "/web-chat/messages",
            "/telegram/webhook",
            "/garmin-auth/start",
        ):
            assert native.post(path, headers=headers("both"), json=NOTE).status_code == 404
        assert native.put(event_path, headers=headers("both"), json=NOTE).status_code == 405
        assert native.delete(event_path, headers=headers("both")).status_code == 405
        with engine.begin() as connection:
            assert not connection.scalar(lock)
    with engine.begin() as connection:
        assert connection.scalar(lock)
    print(
        "Verified read-only inspection before runtime handoff and refusal while the lease is held."
    )


def verify(binary, root, source, before_target, after_target):
    origin = settings(root / "source", source)
    before = settings(root / "before", before_target)
    after = settings(root / "after", after_target)
    health_day = AT.date() + timedelta(days=1)
    with Session(source) as session, session.begin():
        session.add(HealthDay(day=health_day, steps=2345, updated_at=AT))
    with previous_api(origin, source) as previous:
        expected_health = health_snapshot(previous, health_day)
        assert expected_health.status_code == 200, expected_health.text
    with native_api(binary, root, origin, read_only=True) as native:
        restored_health = health_snapshot(native, health_day)
        assert (restored_health.status_code, restored_health.json()) == (
            200,
            expected_health.json(),
        )
    capture(source, origin)
    prepare_state(source)
    old_id = str(table_values(source)["events"][0]["id"])
    old_path = f"{PATH}/{old_id}"
    with previous_api(origin, source) as previous:
        observed = previous.get(old_path, headers=headers("reader"))
        assert observed.status_code == 200
        original_event = observed.json()
        pending_health = health_snapshot(previous, AT.date())
        assert pending_health.status_code == 503

    # The previous API has stopped. Its backup is taken before native writes are admitted.
    first_archive = origin.backup_dir / "before.enc"
    create_backup(source, origin, first_archive)
    before_digest = archive_digest(first_archive)
    initial = table_values(source)
    inspect_before_writes(
        binary, root, origin, source, old_path, original_event, pending_health.json()
    )
    assert table_values(source) == initial
    first_seconds = restore(binary, root, before, first_archive, initial, origin, before_target)
    with previous_api(before, before_target) as recovered_previous:
        assert recovered_previous.get(old_path, headers=headers("reader")).json() == original_event

    request_headers = headers("both", "synthetic-post-cutover-note")
    with native_api(binary, root, origin) as native:
        first = native.post(PATH, json=NOTE, headers=request_headers)
        assert first.status_code == 200
        identity = first.json()["id"]
        path = f"{PATH}/{identity}"
        correction = {
            **NOTE,
            "payload": {**NOTE["payload"], "description": "Synthetic post-transition correction"},
        }
        saved = native.put(path, json={"revision": 1, "event": correction}, headers=request_headers)
        assert saved.status_code == 200 and saved.json()["revision"] == 2
        final_event = saved.json()

    # Freeze the native API first, keeping the old archive intact and making a fresh recovery copy.
    current = table_values(source)
    second_archive = origin.backup_dir / "after.enc"
    run_cli(binary, root, origin, "backup", second_archive)
    assert archive_digest(first_archive) == before_digest
    second_seconds = restore(binary, root, after, second_archive, current, origin, after_target)
    with previous_api(before, before_target) as restored_before:
        assert restored_before.get(path, headers=headers("reader")).status_code == 404
    with native_api(binary, root, after) as restored_after:
        assert restored_after.get(path, headers=headers("reader")).json() == final_event
        replay = restored_after.post(PATH, json=NOTE, headers=request_headers)
        assert replay.status_code == 200 and replay.json() == final_event
        assert (
            restored_after.put(
                path, json={"revision": 1, "event": correction}, headers=request_headers
            ).status_code
            == 409
        )
    check_rows(after_target, current)
    for table in (
        "jobs",
        "outbox_messages",
        "message_delivery_receipts",
        "source_connections",
        "channel_bindings",
    ):
        assert current[table] and current[table] == initial[table], table
    for store in (source, before_target, after_target):
        with store.connect() as connection:
            rows = connection.execute(
                text("SELECT state,attempts FROM outbox_messages ORDER BY state")
            ).all()
            assert rows == [("queued", 0), ("uncertain", 1)], rows
    print("Verified queued and uncertain outbound receipts across both recovery boundaries.")
    print(
        json.dumps(
            {"before_restore_seconds": first_seconds, "after_restore_seconds": second_seconds}
        )
    )
    print(
        "Verified isolated recovery before and after native writes, preserving queued work and receipts"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    addresses = [
        os.environ.get(key, "")
        for key in (
            "GA_TEST_DATABASE_URL",
            "GA_TEST_BEFORE_RESTORE_URL",
            "GA_TEST_AFTER_RESTORE_URL",
        )
    ]
    parsed = list(map(make_url, addresses))
    source = parsed[0]
    assert len({url.database for url in parsed}) == len(parsed)
    assert all(url.database and url.database.endswith("_test") for url in parsed)
    assert all(url == source.set(database=url.database) for url in parsed)
    with ExitStack() as stack:
        root = Path(stack.enter_context(TemporaryDirectory())).resolve()
        engines = [provision_engine(stack, url) for url in addresses]
        verify(args.binary.resolve(strict=True), root, *engines)


if __name__ == "__main__":
    main()
