"""Exercise the native worker with an independent source example and a disposable database."""

import argparse
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_source_extension_poll import (
    CONFIG,
    CURSOR,
    INSTANCE,
    SECRET,
    engine_selection,
    scoped_unfinished_adapter,
    seeded,
)
from check_source_page_storage import snapshot
from sqlalchemy import text

from garmin_ai.config import IntegrationInstance, Settings


@contextmanager
def running_worker(binary, directory, environment):
    with subprocess.Popen(
        [binary, "worker"],
        cwd=directory,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as worker:
        try:
            assert worker.stdout.readline().strip() == "Garmin AI worker ready"
            yield worker
        finally:
            worker.terminate()
            _, errors = worker.communicate(timeout=6)
            assert SECRET not in errors
            assert worker.returncode == 0, errors.replace(SECRET, "<redacted>")


def job(engine, status, attempts, identity=None):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT id,dedup_key,status,attempts,last_error FROM jobs WHERE "
                "kind='source_plugin_poll' AND payload->>'instance_id'=:instance "
                "AND status=:status AND attempts=:attempts "
                "AND (CAST(:id AS uuid) IS NULL OR id=CAST(:id AS uuid)) "
                "ORDER BY run_at,id LIMIT 1"
            ),
            {"instance": INSTANCE, "status": status, "attempts": attempts, "id": identity},
        ).first()


def wait_job(engine, process, status, attempts, identity=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and process.poll() is None:
        if saved := job(engine, status, attempts, identity):
            return saved
        time.sleep(0.03)
    raise AssertionError("The expected source job state was never saved")


def verify(engine, binary, sample, directory):
    instance = IntegrationInstance(
        id=INSTANCE,
        kind="source",
        provider="sample",
        config=CONFIG,
        secret_refs={"fixture": "GA_PLUGIN_SYNTHETIC_SOURCE"},
    )
    seeded(engine, Settings(_env_file=None, integrations=[instance]))
    data = directory / "data"
    (data / "raw").mkdir(parents=True, mode=0o700)
    environment = {
        "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
        "GA_DATA_DIR": str(data),
        "GA_INTEGRATIONS": json.dumps([instance.model_dump(mode="json")]),
        "GA_PLUGIN_SYNTHETIC_SOURCE": SECRET,
        "GA_PLUGIN_UNRELATED": "synthetic_unrelated",
    }

    def configured(executable):
        return {
            **environment,
            "GA_SOURCE_PLUGIN_EXECUTABLES": json.dumps({INSTANCE: str(executable)}),
        }

    with running_worker(binary, directory, environment):
        time.sleep(0.35)
        assert not snapshot(engine)[0]
    with engine_selection(engine, []), running_worker(binary, directory, configured(sample)):
        time.sleep(0.35)
        assert not snapshot(engine)[0]
        with engine.connect() as connection:
            assert not connection.scalar(
                text("SELECT count(*) FROM jobs WHERE kind='source_plugin_poll'")
            )

    broken = scoped_unfinished_adapter(directory, sample)
    with running_worker(binary, directory, configured(broken)) as worker:
        row = wait_job(engine, worker, "pending", 1)
        assert row.last_error == "RuntimeError"
        partial = snapshot(engine)
        assert len(partial[0]) == len(partial[1]) == 2
        assert partial[2][CURSOR]["next_cursor"] == "2"

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET run_at=clock_timestamp() WHERE id=:id AND status='pending'"),
            {"id": row.id},
        )
    with running_worker(binary, directory, configured(sample)) as worker:
        finished = wait_job(engine, worker, "done", 2, row.id)
        assert finished.dedup_key == row.dedup_key
        complete = snapshot(engine)
        assert len(complete[0]) == len(complete[1]) == 3
        assert complete[2][CURSOR]["next_cursor"] is None
        assert complete[2][CURSOR]["last_completed_at"] is not None


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("binary", type=Path)
    cli.add_argument("sample", type=Path)
    args = cli.parse_args()
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as temporary:
            verify(
                engine,
                args.binary.resolve(strict=True),
                args.sample.resolve(strict=True),
                Path(temporary).resolve(),
            )
    finally:
        engine.dispose()
    print("The native worker ran only the selected source and resumed its recorded page")


if __name__ == "__main__":
    main()
