"""Compare ordinary Rust worker trend generation with pinned Python on synthetic days."""

import argparse
import json
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zoneinfo import ZoneInfo

from capture_reference import isolated_engine
from check_native_worker_health import read_heartbeat, wait_ready
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.models import HealthDay, Job, Person
from garmin_ai.proactive import generate_insights
from garmin_ai.scenario_packs import ensure_scenario_packs

SLOT = "insights:native-generation-slot"
ZONE = "Pacific/Auckland"


@contextmanager
def worker(binary, root, engine):
    environment = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_DATA_DIR": str(root),
        "GA_TIMEZONE": "UTC",
        "GA_INTEGRATIONS": "[]",
    }
    with subprocess.Popen(
        [binary, "worker"], cwd=root, env=environment, stdout=subprocess.PIPE, text=True
    ) as process:
        try:
            wait_ready(process)
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                assert process.wait(timeout=5) == 0


def rows(engine, *, full=False):
    columns = "id,generated_at," if full else ""
    with engine.connect() as connection:
        return (
            connection.execute(
                text(
                    f"SELECT {columns}dedup_key,category,statement,evidence,sample_size,effect_size,status"
                    " FROM insights ORDER BY dedup_key"
                )
            )
            .mappings()
            .all()
        )


def execute(engine, query, **arguments):
    with engine.begin() as connection:
        connection.execute(text(query), arguments)


def state(engine, key, document):
    execute(
        engine,
        "INSERT INTO app_state(key,value) VALUES (:key,CAST(:value AS jsonb)) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        key=key,
        value=json.dumps(document),
    )


def slot(engine):
    with engine.connect() as connection:
        return connection.scalar(text("SELECT value FROM app_state WHERE key=:key"), {"key": SLOT})


def await_slot(engine, process):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert process.poll() is None
        if slot(engine) is not None:
            return
        time.sleep(0.1)
    raise AssertionError("The worker did not complete the trend generation cycle")


def reference(engine, at):
    with Session(engine) as session, session.begin():
        generate_insights(session, at, ZONE)
    return rows(engine)


def seed(engine, at):
    today = at.astimezone(ZoneInfo(ZONE)).date()
    with Session(engine) as session, session.begin():
        session.query(Person).one().timezone = ZONE
        for offset in range(29):
            session.add(
                HealthDay(
                    day=today - timedelta(days=offset),
                    sleep_score=99 if offset == 0 else 75 if offset <= 14 else 65,
                    sleep_seconds=25200,
                    hrv_nightly_avg=None if offset == 19 else 50,
                    resting_hr=60,
                    stress_avg=35,
                )
            )
        packs = ensure_scenario_packs(session, legacy_install=True)
        packs["sleep"].reminders_enabled = True
        packs["wellbeing"].reminders_enabled = True
    state(engine, "preferences:onboarding", {"model_categories": [], "timezone": ZONE})


def verify(binary, root, engine):
    (root / "raw").mkdir(mode=0o700)
    now = datetime.now(UTC)
    seed(engine, now)
    expected = reference(engine, now)
    assert len(expected) == 5
    accepted = [record for record in expected if record["status"] == "accepted"]
    assert len(accepted) == 1 and ":sleep_score:" in accepted[0]["dedup_key"]
    assert accepted[0]["sample_size"] == 28 and accepted[0]["evidence"]["difference"] == 10.0
    execute(engine, "DELETE FROM insights")
    pending = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            Job(
                id=pending,
                kind="garmin_endpoint",
                payload={"endpoint": "sleep", "key": "synthetic"},
                dedup_key="synthetic-trend-waits-for-sync",
                status="pending",
                run_at=now,
            )
        )
    with engine.begin() as replay:
        replay.execute(text("SELECT pg_advisory_xact_lock(72104619)"))
        with worker(binary, root, engine) as process:
            assert read_heartbeat(engine)
            assert rows(engine) == [] and slot(engine) is None
    with worker(binary, root, engine) as process:
        assert read_heartbeat(engine)
        time.sleep(0.3)
        assert rows(engine) == [] and slot(engine) is None
        execute(engine, "UPDATE jobs SET status='done' WHERE id=:id", id=pending)
        await_slot(engine, process)
    assert rows(engine) == expected, (rows(engine), expected)
    first_native = rows(engine, full=True)

    execute(engine, "UPDATE health_days SET sleep_score=70")
    with worker(binary, root, engine):
        time.sleep(0.3)
        assert rows(engine, full=True) == first_native

    execute(engine, "DELETE FROM app_state WHERE key=:key", key=SLOT)
    state(engine, "integration:garmin", {"status": "reauth_required"})
    with worker(binary, root, engine) as process:
        await_slot(engine, process)
    assert rows(engine, full=True) == first_native
    execute(engine, "DELETE FROM app_state WHERE key IN (:slot,'integration:garmin')", slot=SLOT)
    state(engine, "proactive:enabled", {"enabled": False})
    with worker(binary, root, engine) as process:
        await_slot(engine, process)
    assert rows(engine, full=True) == first_native
    execute(engine, "DELETE FROM app_state WHERE key IN (:slot,'proactive:enabled')", slot=SLOT)

    execute(
        engine,
        "UPDATE insights SET status='delivered' WHERE starts_with(dedup_key,'trend:resting_hr:')",
    )
    execute(
        engine,
        "UPDATE insights SET status='uncertain' WHERE starts_with(dedup_key,'trend:sleep_seconds:')",
    )
    state(engine, "insight:last:stress_avg", {"at": now.isoformat()})
    with worker(binary, root, engine) as process:
        await_slot(engine, process)
    updated = rows(engine, full=True)
    assert len(updated) == 5
    for initial, latest in zip(first_native, updated, strict=True):
        assert initial["id"] == latest["id"]
        metric = latest["dedup_key"].split(":")[1]
        if metric == "sleep_score":
            assert latest["status"] == "candidate" and latest["evidence"]["difference"] == 0.0
        elif metric == "sleep_seconds":
            assert (
                latest["status"] == "uncertain"
                and latest["generated_at"] == initial["generated_at"]
            )
        elif metric == "resting_hr":
            assert (
                latest["status"] == "delivered"
                and latest["generated_at"] == initial["generated_at"]
            )
        elif metric == "stress_avg":
            assert latest == initial
        else:
            assert latest["evidence"] == initial["evidence"]
            assert latest["generated_at"] > initial["generated_at"]
    execute(engine, "DELETE FROM app_state WHERE key=:key", key=SLOT)
    execute(engine, "UPDATE module_configs SET reminders_enabled=false WHERE pack_key='wellbeing'")
    with worker(binary, root, engine) as process:
        await_slot(engine, process)
    assert [row for row in rows(engine, full=True) if ":hrv_nightly_avg:" in row["dedup_key"]] == [
        row for row in updated if ":hrv_nightly_avg:" in row["dedup_key"]
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            verify(args.binary.resolve(strict=True), Path(directory), engine)
    finally:
        engine.dispose()
    print(
        "Matched weekly trend evidence and records, deferred busy sync, and preserved restart state"
    )


if __name__ == "__main__":
    main()
