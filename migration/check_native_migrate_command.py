"""Compare the public migrate command with pinned Python registry initialization."""

import argparse
import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID

from check_legacy_telegram_schema import BEFORE_REPAIR as BEFORE_TELEGRAM_BACKFILL
from check_legacy_telegram_schema import seed as seed_telegram_backfill
from check_native_schema import (
    BEFORE_FENCE,
    BEFORE_INGESTION,
    BEFORE_REVISIONS,
    CURRENT,
    compare_catalog,
    engine_for,
    upgrade,
)
from check_telegram_schema import BEFORE_REPAIR as BEFORE_TELEGRAM_REPAIR
from check_telegram_schema import seed as seed_telegram_repair
from check_telegram_schema import transported_rows
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.models import Event

CASES = (
    ("fresh", None),
    ("telegram_backfill", BEFORE_TELEGRAM_BACKFILL),
    ("telegram_repair", BEFORE_TELEGRAM_REPAIR),
    ("ingestion", BEFORE_INGESTION),
    ("revisions", BEFORE_REVISIONS),
    ("delivery_fence", BEFORE_FENCE),
    ("current", CURRENT),
)

REJECTED_STATES = (
    ("occupied", None),
    ("unknown", ("unrecognized-fixture",)),
    ("split_ledger", (BEFORE_FENCE, CURRENT)),
    ("empty_ledger", ()),
)


REGISTRY = (
    """
    SELECT d.key,d.status,d.current_version,d.revision,v.version,v.schema_hash,
           v.schema,v.topology,v.field_metadata,v.labels,v.privacy,v.allowed_operations
    FROM event_definitions d JOIN event_definition_versions v ON v.definition_id=d.id
    ORDER BY d.key,v.version
    """,
    """
    SELECT d.key,d.status,d.current_version,v.version,v.schema_hash,
           v.value_kind,v.unit,v.dimension,v.aggregation,v.allowed_methods,v.coverage_policy,
           v.time_semantics,v.category_domain
    FROM metric_definitions d JOIN metric_definition_versions v ON v.definition_id=d.id
    ORDER BY d.key,v.version
    """,
    """
    SELECT pack_key,tracking_enabled,collection_enabled,reminders_enabled,visible,llm_enabled,
           outcome_goal,settings,revision
    FROM module_configs ORDER BY pack_key
    """,
    """
    SELECT key,value FROM app_state WHERE key LIKE 'registry:%' ORDER BY key
    """,
    """
    SELECT event.kind,event.payload,catalog.key,version.version,version.schema_hash
    FROM events event LEFT JOIN event_definition_versions version ON version.id=event.definition_version_id
    LEFT JOIN event_definitions catalog ON catalog.id=version.definition_id ORDER BY event.id
    """,
    """
    SELECT m.metric,d.key,m.value,m.ingested_at
    FROM measurements m LEFT JOIN metric_definition_versions v ON v.id=m.metric_definition_version_id
    LEFT JOIN metric_definitions d ON d.id=v.definition_id ORDER BY m.ts,m.metric
    """,
)


def database_names(prefix):
    labels = [f"{label}_{kind}" for label, _ in CASES for kind in ("py", "rs")]
    labels.extend(("erased", "invalid", "interrupted"))
    labels.extend(label for label, _ in REJECTED_STATES)
    return [f"{prefix}_{label}_test" for label in labels]


def migration_env(engine, root):
    return {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_LOCK_DIR": str(root / "locks"),
    }


def command(binary, engine, root, *args, succeeds=True):
    result = subprocess.run(
        (binary, "migrate", *args),
        env=migration_env(engine, root),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if succeeds:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1 and not result.stdout
        return result.stderr.strip()
    return result.stdout.strip()


def bootstrap_snapshot(engine):
    with engine.connect() as connection:
        return tuple(connection.execute(text(statement)).all() for statement in REGISTRY)


def seed(engine, revision=BEFORE_REVISIONS, *, invalid=False):
    if revision == BEFORE_TELEGRAM_BACKFILL:
        seed_telegram_backfill(engine, "binding")
    elif revision == BEFORE_TELEGRAM_REPAIR:
        seed_telegram_repair(engine)
    else:
        upgrade(engine, revision)
    at = datetime(2026, 11, 1, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            Event(
                id=UUID(int=200),
                kind="symptom_observation",
                start=at,
                timezone="UTC",
                source="manual",
                payload={
                    "type": "symptom_observation",
                    "episode_id": str(UUID(int=198)),
                    "impact": "   ",
                }
                if not invalid
                else {"type": "symptom_observation", "impact": ""},
            )
        )
        source = UUID(int=201)
        session.execute(
            text("""
            INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,archive_key,
                                       fetched_at,parser_version,status)
            VALUES (:source,'synthetic','migration','source','fixture-digest','fixture-object',
                    :at,1,'ok')
            """),
            {"at": at, "source": source},
        )
        ingested = revision in (BEFORE_REVISIONS, BEFORE_FENCE, CURRENT)
        columns = ",ingested_at" if ingested else ""
        values = ",:at" if ingested else ""
        session.execute(
            text(f"""
            INSERT INTO measurements(ts,metric,source,local_date,value,unit,quality,details,source_ref{columns})
            VALUES (:at,'heart_rate_bpm','synthetic',CAST(:at AS date),64,'bpm','observed','{{}}',:source{values})
            """),
            {"at": at, "source": source},
        )


def verify_ready(binary, engine, root):
    key = "synthetic-readiness-only-token-at-least-32"
    with subprocess.Popen(
        (binary, "serve", "--port", "0"),
        env=migration_env(engine, root) | {"GA_API_KEY": key},
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as server:
        try:
            assert select.select([server.stdout], [], [], 5)[0]
            address = server.stdout.readline().strip().removeprefix("Listening on ")
            assert address.startswith("http://127.0.0.1:")
            request = Request(f"{address}/health/ready", headers={"Authorization": f"Bearer {key}"})
            with build_opener(ProxyHandler({})).open(request, timeout=4) as response:
                assert response.status == 200
        finally:
            if server.poll() is None:
                server.terminate()
            try:
                _, error = server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate(timeout=3)
                raise
            assert server.returncode == 0, error


def compare(binary, reference, native, root, revision):
    if revision:
        seed(reference, revision)
        seed(native, revision)
    assert json.loads(command(binary, native, root, "--status")) == {
        "current": revision == CURRENT,
        "revision": revision,
    }
    assert not (root / "locks").exists()
    legacy = Path(sys.executable).with_name("garmin-ai")
    assert command(legacy, reference, root) == "Database schema upgraded."
    assert command(binary, native, root) == "Database schema upgraded."
    assert json.loads(command(binary, native, root, "--status")) == {
        "current": True,
        "revision": CURRENT,
    }
    expected = bootstrap_snapshot(reference)
    actual = bootstrap_snapshot(native)
    for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
        assert left == right, (index, len(left), len(right))
    compare_catalog(reference, native, semantic_metric_versions=True)
    assert transported_rows(reference) == transported_rows(native)
    assert tuple(map(len, actual[:3])) == (20 if revision else 19, 11, 6)
    if revision:
        event = actual[4][0]
        assert event[2] == "system.symptom_observation" and event[3] != 1
        assert actual[5][0][1] == "system.heart_rate_bpm"
    command(binary, native, root)
    assert bootstrap_snapshot(native) == actual
    verify_ready(binary, native, root)
    print(f"Matched the pinned CLI and verified readiness from {revision or 'an empty database'}")


def interrupt_upgrade(binary, engine, root):
    waiting = text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE database=(SELECT oid FROM pg_database WHERE datname=current_database())
              AND relation='public.outbox_messages'::regclass
              AND mode='AccessExclusiveLock' AND NOT granted
        )
        """)
    with engine.begin() as blocker:
        blocker.execute(text("LOCK TABLE public.outbox_messages IN ACCESS SHARE MODE"))
        with subprocess.Popen(
            (binary, "migrate"),
            env=migration_env(engine, root),
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                deadline = time.monotonic() + 1.8
                while not blocker.scalar(waiting):
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(0.01)
                process.kill()
                output, error = process.communicate(timeout=4)
                assert process.returncode == -signal.SIGKILL and not output, (output, error)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=4)
    assert json.loads(command(binary, engine, root, "--status")) == {
        "current": False,
        "revision": BEFORE_REVISIONS,
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('measurement_revisions') IS NULL"))
        assert connection.scalar(text("SELECT value FROM measurements")) == 64
    command(binary, engine, root)
    assert json.loads(command(binary, engine, root, "--status"))["revision"] == CURRENT
    verify_ready(binary, engine, root)
    print("Killed a migration before the final schema change, verified rollback, then retried")


def verify_rejected_schema(binary, engine, root, revisions):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated_fixture(id integer PRIMARY KEY)"))
        connection.execute(text("INSERT INTO unrelated_fixture VALUES (17)"))
        if revisions is not None:
            connection.execute(
                text("CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY)")
            )
            for revision in revisions:
                connection.execute(
                    text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision}
                )
    reason = (
        "A database without a Garmin AI revision must be empty"
        if revisions is None
        else "The recorded database revision is unsupported"
    )
    assert command(binary, engine, root, "--status", succeeds=False) == reason
    assert not (root / "locks").exists()
    assert command(binary, engine, root, succeeds=False) == reason
    tables = {"unrelated_fixture"}
    with engine.connect() as connection:
        if revisions is not None:
            tables.add("alembic_version")
            assert set(connection.scalars(text("SELECT version_num FROM alembic_version"))) == set(
                revisions
            )
        assert set(inspect(connection).get_table_names(schema="public")) == tables
        assert connection.execute(text("SELECT id FROM unrelated_fixture")).scalars().all() == [17]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("name_prefix")
    parser.add_argument("--list-databases", action="store_true")
    args = parser.parse_args()
    if args.list_databases:
        print("\n".join(database_names(args.name_prefix)))
        return
    binary = args.binary.resolve(strict=True)
    opening = make_url(os.environ["GA_TEST_DATABASE_URL"])
    assert opening.database and opening.database.endswith("_test")
    with ExitStack() as stack:
        for label, revision in CASES:
            reference, native = [
                engine_for(stack, opening.set(database=f"{args.name_prefix}_{label}_{kind}_test"))
                for kind in ("py", "rs")
            ]
            with TemporaryDirectory() as root:
                compare(
                    binary,
                    reference,
                    native,
                    Path(root).resolve(),
                    revision,
                )
        erased = engine_for(stack, opening.set(database=f"{args.name_prefix}_erased_test"))
        upgrade(erased, BEFORE_REVISIONS)
        with erased.begin() as connection:
            connection.execute(
                text("INSERT INTO app_state(key,value) VALUES ('maintenance:erased','{}')")
            )
        with TemporaryDirectory() as root:
            command(binary, erased, Path(root).resolve())
        with erased.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT
            assert connection.scalar(text("SELECT key FROM app_state")) == "maintenance:erased"
            assert connection.scalar(text("SELECT count(*) FROM event_definitions")) == 0
        invalid = engine_for(stack, opening.set(database=f"{args.name_prefix}_invalid_test"))
        seed(invalid, invalid=True)
        with TemporaryDirectory() as root:
            command(binary, invalid, Path(root).resolve(), succeeds=False)
        with invalid.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == BEFORE_REVISIONS
            )
            assert connection.scalar(text("SELECT to_regclass('measurement_revisions') IS NULL"))
            assert connection.scalar(text("SELECT count(*) FROM event_definitions")) == 0
        interrupted = engine_for(
            stack, opening.set(database=f"{args.name_prefix}_interrupted_test")
        )
        seed(interrupted)
        with TemporaryDirectory() as root:
            interrupt_upgrade(binary, interrupted, Path(root).resolve())
        for label, revisions in REJECTED_STATES:
            target = engine_for(stack, opening.set(database=f"{args.name_prefix}_{label}_test"))
            with TemporaryDirectory() as root:
                verify_rejected_schema(binary, target, Path(root).resolve(), revisions)
            print(f"The public migrator refused the {label} schema and preserved its records")
    print("Erased storage stayed fenced and invalid history rolled back")


if __name__ == "__main__":
    main()
