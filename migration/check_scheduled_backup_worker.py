"""Verify scheduled backup publication, restart recovery and retention."""

import argparse
import base64
import subprocess
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_backup_commands import capture, check_recovered_files, cli_environment, run_cli, settings
from check_native_worker_health import wait_ready
from sqlalchemy import text


@contextmanager
def running_worker(binary, root, config, **overrides):
    environment = (
        cli_environment(config)
        | {
            "GA_INTEGRATIONS": "[]",
            "GA_BACKUP_KEEP_DAILY": "2",
        }
        | overrides
    )
    with subprocess.Popen(
        [binary, "worker"],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
    ) as worker:
        try:
            wait_ready(worker)
            yield worker
        finally:
            if worker.poll() is None:
                worker.terminate()
                assert worker.wait(timeout=5) == 0


def wait_for_backup(engine, worker, key, status, attempts):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert worker.poll() is None
        with engine.connect() as connection:
            state = connection.execute(
                text("""
                    SELECT j.status,j.attempts,s.value,j.last_error
                    FROM jobs j LEFT JOIN app_state s ON s.key='backup:last_success'
                    WHERE j.dedup_key=:key
                """),
                {"key": key},
            ).one_or_none()
        if state and (state.status, state.attempts) == (status, attempts):
            return state.value, state.last_error
        time.sleep(0.15)
    raise AssertionError(f"Backup job did not reach {status} at attempt {attempts}: {state!r}")


def rerun(engine, key):
    with engine.begin() as connection:
        connection.execute(
            text("""
                UPDATE jobs SET status='pending',run_at=clock_timestamp(),completed_at=NULL,
                  lease_until=NULL,lease_token=NULL,attempts=0,last_error=NULL
                WHERE dedup_key=:key
            """),
            {"key": key},
        )
        connection.execute(text("DELETE FROM app_state WHERE key='backup:last_success'"))


def verify(binary, root, engine):
    config = settings(root, engine)
    raw, _ = capture(engine, config)
    raw.chmod(0o600)
    raw.parent.chmod(0o700)
    (config.data_dir / "raw").chmod(0o700)
    config.backup_dir.mkdir(mode=0o700)
    date = datetime.now(UTC).date()
    key = f"backup:{date}"
    archive = config.backup_dir / f"garmin-ai-{date}.enc"
    with running_worker(binary, root, config) as worker:
        first, error = wait_for_backup(engine, worker, key, "done", 1)
        assert first["path"] == str(archive) and error is None
    assert archive.stat().st_mode & 0o777 == 0o600
    recovered = root / "scheduled-recovered"
    assert run_cli(binary, root, config, "unpack-backup", archive, recovered) == "Backup unpacked."
    check_recovered_files(recovered, config)
    assert (recovered / "database.jsonl.gz").stat().st_size > 0
    original = (archive.read_bytes(), archive.stat().st_mtime_ns)

    recent, old, link = [
        config.backup_dir / f"garmin-ai-{date - timedelta(days=days)}.enc" for days in (1, 2, 3)
    ]
    recent.write_bytes(b"Recent synthetic bundle")
    old.write_bytes(b"Old synthetic bundle")
    unrelated = config.backup_dir / "operator-notes.txt"
    unrelated.write_bytes(b"Unrelated synthetic file")
    link.symlink_to(unrelated)
    rerun(engine, key)
    wrong_key = base64.urlsafe_b64encode(b"x" * 32).decode()
    with running_worker(binary, root, config, GA_BACKUP_KEY=wrong_key) as worker:
        success, error = wait_for_backup(engine, worker, key, "pending", 1)
        assert success is None and error
    assert old.is_file() and recent.is_file() and link.is_symlink()
    assert (archive.read_bytes(), archive.stat().st_mtime_ns) == original

    rerun(engine, key)
    with running_worker(binary, root, config) as worker:
        reused, error = wait_for_backup(engine, worker, key, "done", 1)
        assert error is None and reused == first
    assert (archive.read_bytes(), archive.stat().st_mtime_ns) == original
    assert recent.is_file() and not old.exists() and link.is_symlink()
    assert unrelated.read_bytes() == b"Unrelated synthetic file"
    print("Verified scheduled encrypted backup, wrong-key retry, unchanged reuse and retention")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            verify(args.binary.resolve(strict=True), Path(directory).resolve(), engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
