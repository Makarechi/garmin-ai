"""Rehearse worker ownership across Python, Rust and a return to Python."""

import argparse
import subprocess
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from capture_reference import isolated_engine
from check_cutover_restore import prepare_state
from check_database_export import SOURCE, table_values
from replay_workload import projection_digest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sync_workload import DAY, SyntheticReader, configure_synthetic_owner

from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.models import AppState, Job, SourcePayload
from garmin_ai.replay import PARSER_VERSION, schedule_replay
from garmin_ai.sync import run_garmin_job

LEASE = """
SELECT pid
FROM pg_locks
WHERE locktype = 'advisory' AND granted AND classid = 0
  AND objid = 72104620 AND objsubid = 1
  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""
REPLAY_WRITE_LOCK = 72104619


def lease(engine):
    with engine.connect() as connection:
        return connection.execute(text(LEASE)).scalar_one_or_none()


def ready(engine, process, *, native):
    deadline = monotonic() + 20
    while monotonic() < deadline:
        assert process.poll() is None, f"Worker exited with status {process.returncode}"
        with engine.connect() as connection:
            current = connection.execute(text(LEASE)).scalar_one_or_none()
            pulse = connection.execute(
                text("SELECT value FROM app_state WHERE key='runtime:heartbeat'")
            ).scalar_one_or_none()
        if current is not None and pulse is not None:
            if native and pulse.get("native_pid") == current:
                return current
            if not native and "native_pid" not in pulse:
                return current
        sleep(0.1)
    raise AssertionError("The expected worker did not publish a matching heartbeat")


@contextmanager
def worker(command, root, environment):
    with subprocess.Popen(
        [command, "worker"],
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise


def assert_refused(command, root, environment, diagnostic):
    result = subprocess.run(
        [command, "worker"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and diagnostic in result.stderr, result.returncode


def preserved_state(engine):
    data = table_values(engine)
    relevant = {"telegram:offset", "conversation:pending", "replay:source_scan_cursor"}
    return {
        "state": [row for row in data["app_state"] if row["key"] in relevant],
        "jobs": [row for row in data["jobs"] if row["dedup_key"] == f"raw-replay:{SOURCE}:9"],
        "outbox": [row for row in data["outbox_messages"] if row["state"] == "uncertain"],
        "receipts": data["message_delivery_receipts"],
        "source_bindings": data["source_connections"],
        "channel_bindings": data["channel_bindings"],
    }


def worker_environments(engine, root):
    (root / "data" / "raw").mkdir(parents=True, mode=0o700)
    environment = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_DATA_DIR": str(root / "data"),
        "GA_TOKEN_DIR": str(root / "tokens"),
        "GA_INTEGRATIONS": "[]",
        "GA_TIMEZONE": "UTC",
    }
    return tuple(
        {**environment, "GA_LOCK_DIR": str(root / name)} for name in ("python-locks", "rust-locks")
    )


def verify(engine, root, previous, candidate):
    prepare_state(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET run_at=clock_timestamp() + interval '1 day' WHERE dedup_key=:key"
            ),
            {"key": f"raw-replay:{SOURCE}:9"},
        )
    initial = preserved_state(engine)
    assert all(initial.values()) and len(initial["state"]) == 3
    python_env, rust_env = worker_environments(engine, root)

    with worker(previous, root, python_env) as process:
        original = ready(engine, process, native=False)
        assert_refused(candidate, root, rust_env, "Another Garmin AI worker is already running")
        assert lease(engine) == original and preserved_state(engine) == initial
        process.kill()
        assert process.wait(timeout=5) < 0

    with worker(candidate, root, rust_env) as process:
        native = ready(engine, process, native=True)
        assert native != original
        assert_refused(
            previous,
            root,
            python_env,
            "Operation failed (RuntimeError). No sensitive details logged.",
        )
        assert lease(engine) == native and preserved_state(engine) == initial
        process.terminate()
        assert process.wait(timeout=15) == 0
    assert lease(engine) is None

    with worker(previous, root, python_env) as process:
        returned = ready(engine, process, native=False)
        assert returned not in (original, native)
        assert preserved_state(engine) == initial
    assert lease(engine) is None and preserved_state(engine) == initial


def archived_backlog(engine, root):
    settings = Settings(_env_file=None, data_dir=root / "data", timezone="UTC", integrations=[])
    archive = LocalArchive(settings.data_dir / "raw")
    reader = SyntheticReader()
    configure_synthetic_owner(engine, reader, archive)
    for endpoint in ("daily", "heart_rate"):
        run_garmin_job(
            engine,
            reader,
            archive,
            settings,
            "garmin_endpoint",
            {"endpoint": endpoint, "key": DAY.isoformat()},
        )
    with Session(engine) as session, session.begin():
        sources = list(session.scalars(select(SourcePayload).order_by(SourcePayload.id)))
        assert len(sources) == 2 and all(row.parser_version == PARSER_VERSION for row in sources)
        initial = {row.id: row.status for row in sources}
        digest = projection_digest(session)
        for row in sources:
            row.parser_version -= 1
        session.flush()
        assert schedule_replay(session, datetime.now(UTC)) == len(sources)
    return initial, digest


def replay_jobs(engine):
    with Session(engine) as session:
        return {job.id: job for job in session.scalars(select(Job).where(Job.kind == "raw_replay"))}


def wait_for_jobs(engine, process, condition, *, seconds, failure):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        assert process.poll() is None, f"Worker exited with status {process.returncode}"
        jobs = replay_jobs(engine)
        if condition(jobs):
            return jobs
        sleep(0.2)
    raise AssertionError(failure)


def verify_replay_backlog(engine, root, previous, candidate):
    python_env, rust_env = worker_environments(engine, root)
    sources, digest = archived_backlog(engine, root)
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": REPLAY_WRITE_LOCK})
        with worker(previous, root, python_env) as process:
            original = ready(engine, process, native=False)
            jobs = wait_for_jobs(
                engine,
                process,
                lambda rows: sorted(job.status for job in rows.values()) == ["pending", "running"],
                seconds=20,
                failure="Python did not claim the first replay job",
            )
            interrupted = next(job for job in jobs.values() if job.status == "running")
            pending = next(job for job in jobs.values() if job.status == "pending")
            assert interrupted.attempts == 1 and interrupted.lease_token is not None
            assert interrupted.lease_until > datetime.now(UTC) + timedelta(seconds=30)
            process.kill()
            assert process.wait(timeout=5) < 0
    # The server can still finish rolling back its disconnected Python transaction.
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '5s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": REPLAY_WRITE_LOCK})
    with ExitStack() as workers:
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock_shared(:lock)"), {"lock": REPLAY_WRITE_LOCK}
            )
            process = workers.enter_context(worker(candidate, root, rust_env))
            native = ready(engine, process, native=True)
            assert native != original
            returned = wait_for_jobs(
                engine,
                process,
                lambda rows: rows[pending.id].last_error == "ReplayBusy",
                seconds=20,
                failure="A busy Rust replay did not defer with its attempt refunded",
            )[pending.id]
            assert returned.status == "pending" and returned.attempts == 0
            assert returned.lease_token is None and returned.completed_at is None
        jobs = wait_for_jobs(
            engine,
            process,
            lambda rows: rows[pending.id].status == "done",
            seconds=20,
            failure="Rust did not drain pending replay while the old claim was live",
        )
        assert datetime.now(UTC) < interrupted.lease_until
        assert jobs[pending.id].attempts == 1
        assert jobs[interrupted.id].lease_token == interrupted.lease_token
        assert jobs[interrupted.id].attempts == 1 and jobs[interrupted.id].status == "running"
        print("Rust drained pending replay and left the live Python claim untouched", flush=True)
        jobs = wait_for_jobs(
            engine,
            process,
            lambda rows: rows[interrupted.id].status == "done",
            seconds=310,
            failure="Rust did not resume the interrupted replay after lease expiry",
        )
        assert set(jobs) == {pending.id, interrupted.id}
        assert jobs[interrupted.id].attempts == 2
        assert jobs[interrupted.id].completed_at >= interrupted.lease_until
        assert all(job.lease_token is None and job.lease_until is None for job in jobs.values())
        assert all(job.last_error is None for job in jobs.values())
        assert lease(engine) == native
    assert lease(engine) is None

    with worker(previous, root, python_env) as process:
        assert ready(engine, process, native=False) not in (original, native)
        assert {key: job.attempts for key, job in replay_jobs(engine).items()} == {
            pending.id: 1,
            interrupted.id: 2,
        }
        with Session(engine) as session:
            assert projection_digest(session) == digest
            for identity, status in sources.items():
                row = session.get(SourcePayload, identity)
                assert row.parser_version == PARSER_VERSION and row.status == status
                outcome = session.get(AppState, f"replay:{identity}:{PARSER_VERSION}")
                assert outcome.value["status"] in {"normalized", "unchanged"}
    assert lease(engine) is None
    print("Verified expired claim recovery, unchanged projections and Python return")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("previous", type=Path, help="Pinned Python garmin-ai CLI")
    parser.add_argument("candidate", type=Path, help="Native garmin-ai-rust CLI")
    parser.add_argument("--replay-backlog", action="store_true", help="Exercise real archive jobs")
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory(prefix="synthetic-worker-cutover-") as directory:
            exercise = verify_replay_backlog if args.replay_backlog else verify
            exercise(
                engine,
                Path(directory).resolve(),
                args.previous.resolve(strict=True),
                args.candidate.resolve(strict=True),
            )
    finally:
        engine.dispose()
    if not args.replay_backlog:
        print(
            "Verified single worker ownership, sudden Python exit, Rust takeover and Python return"
        )


if __name__ == "__main__":
    main()
