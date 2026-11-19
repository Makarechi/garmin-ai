"""Rehearse worker ownership across Python, Rust and a return to Python."""

import argparse
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from capture_reference import isolated_engine
from check_cutover_restore import prepare_state
from check_database_export import SOURCE, table_values
from sqlalchemy import text

LEASE = """
SELECT pid
FROM pg_locks
WHERE locktype = 'advisory' AND granted AND classid = 0
  AND objid = 72104620 AND objsubid = 1
  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""


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
    (root / "data" / "raw").mkdir(parents=True, mode=0o700)
    environment = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_DATA_DIR": str(root / "data"),
        "GA_TOKEN_DIR": str(root / "tokens"),
        "GA_INTEGRATIONS": "[]",
        "GA_TIMEZONE": "UTC",
    }
    python_env = {**environment, "GA_LOCK_DIR": str(root / "python-locks")}
    rust_env = {**environment, "GA_LOCK_DIR": str(root / "rust-locks")}

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("previous", type=Path, help="Pinned Python garmin-ai CLI")
    parser.add_argument("candidate", type=Path, help="Native garmin-ai-rust CLI")
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory(prefix="synthetic-worker-cutover-") as directory:
            verify(
                engine,
                Path(directory).resolve(),
                args.previous.resolve(strict=True),
                args.candidate.resolve(strict=True),
            )
    finally:
        engine.dispose()
    print("Verified single worker ownership, sudden Python exit, Rust takeover and Python return")


if __name__ == "__main__":
    main()
