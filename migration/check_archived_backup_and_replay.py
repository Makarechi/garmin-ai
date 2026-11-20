"""Acquire, back up, restore and replay an identical synthetic Garmin archive."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from check_backup_commands import (
    check_recovered_files,
    cli_environment,
    provision_engine,
    run_cli,
    settings,
)
from check_database_export import table_values
from paired_replay_workload import verify as verify_replay
from paired_replay_workload import wait as wait_replay
from replay_workload import projection_digest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sync_workload import DAY, SyntheticReader, configure_synthetic_owner

from garmin_ai import operations
from garmin_ai.archive import LocalArchive
from garmin_ai.db import transaction
from garmin_ai.garmin_contract import ENDPOINTS
from garmin_ai.models import SourcePayload
from garmin_ai.sync import run_garmin_job


def measured_archive(engine, archive):
    archives = {}
    with transaction(engine) as tx:
        for row in tx.scalars(
            select(SourcePayload).order_by(
                SourcePayload.source, SourcePayload.endpoint, SourcePayload.source_key
            )
        ):
            actual = hashlib.sha256(archive.read(row.archive_key)).hexdigest()
            assert actual == row.payload_hash
            archives[row.archive_key] = actual
    return archives


def run_worker(binary, config, database, archive, fixture, root):
    environment = cli_environment(config)
    environment.update(GA_TIMEZONE="UTC", GA_INTEGRATIONS="[]", GA_LLM_ENABLED="false")
    environment.pop("GA_BACKUP_KEY")
    with (root / f"{binary.name}-worker.log").open("w") as output:
        with subprocess.Popen(
            [binary, "worker"], cwd=root, env=environment, stdout=output, stderr=output
        ) as worker:
            try:
                status = wait_replay(database, fixture, worker=worker)
                return {"status": status, **verify_replay(database, archive, fixture)}
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    try:
                        worker.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait()


def exercise(binary, root, origin, legacy, native, days):
    source = settings(root / "source", origin).model_copy(update={"timezone": "UTC"})
    source.token_dir.mkdir(parents=True)
    (source.token_dir / "synthetic").write_bytes(b"synthetic raw archive fixture token")
    (source.data_dir / "coverage-report.json").parent.mkdir(parents=True, exist_ok=True)
    (source.data_dir / "coverage-report.json").write_bytes(b'{"source":"synthetic fixture"}')
    archive = LocalArchive(source.data_dir / "raw")
    reader = SyntheticReader()
    configure_synthetic_owner(origin, reader, archive)
    endpoints = [endpoint.name for endpoint in ENDPOINTS if endpoint.scope == "day"]
    for offset in range(days):
        day = (DAY - timedelta(days=offset)).isoformat()
        for endpoint in endpoints:
            run_garmin_job(
                origin,
                reader,
                archive,
                source,
                "garmin_endpoint",
                {"endpoint": endpoint, "key": day},
            )
    with transaction(origin) as tx:
        rows = list(tx.scalars(select(SourcePayload)))
        assert len(rows) == days * len(endpoints)
        selected = next(
            row
            for row in rows
            if row.endpoint == "training_status" and row.source_key == DAY.isoformat()
        )
        bad_key, bad_id = selected.archive_key, str(selected.id)
        expected_projection = projection_digest(tx)
        for row in rows:
            row.parser_version = 0
    raw = measured_archive(origin, archive)
    original_state = table_values(origin)
    encrypted = source.backup_dir / "mixed.enc"
    assert len(json.loads(run_cli(binary, root, source, "backup", encrypted))) == 30
    encrypted_sha = hashlib.sha256(encrypted.read_bytes()).hexdigest()
    outcomes = {}
    for label, database, worker_binary in (
        ("python", legacy, Path(sys.executable).with_name("garmin-ai")),
        ("rust", native, binary),
    ):
        target = settings(root / label, database).model_copy(update={"timezone": "UTC"})
        recovery = root / ("recover-" + label)
        if label == "rust":
            assert (
                run_cli(binary, root, source, "unpack-backup", encrypted, recovery)
                == "Backup unpacked."
            )
            assert (
                len(
                    json.loads(
                        run_cli(binary, root, target, "restore-db", recovery / "database.jsonl.gz")
                    )
                )
                == 30
            )
        else:
            operations.unpack_backup(source, encrypted, recovery)
            assert len(operations.restore_database(database, recovery / "database.jsonl.gz")) == 30
        check_recovered_files(recovery, source)
        assert table_values(database) == original_state
        target_archive = LocalArchive(target.data_dir / "raw")
        assert not any(target_archive.root.iterdir())
        shutil.copytree(recovery / "raw", target_archive.root, dirs_exist_ok=True)
        assert measured_archive(database, target_archive) == raw
        with transaction(database) as tx:
            target_clock = tx.scalar(select(func.clock_timestamp())).isoformat()
        (target_archive.root / bad_key).write_bytes(b"deliberately damaged benchmark archive")
        fixture = {
            "archive_objects": len(rows),
            "damaged_id": bad_id,
            "before_projection": expected_projection,
            "observed_start": target_clock,
        }
        outcome = run_worker(worker_binary, target, database, target_archive, fixture, root)
        outcome["status"].pop("queue_and_startup_seconds")
        outcomes[label] = outcome
    assert outcomes["python"] == outcomes["rust"], outcomes
    assert measured_archive(origin, archive) == raw
    assert hashlib.sha256(encrypted.read_bytes()).hexdigest() == encrypted_sha
    print(
        json.dumps(
            {
                "archive_rows": len(rows),
                "endpoints": len(endpoints),
                "paired_result": outcomes["rust"],
                "encrypted_backup_and_original_preserved": True,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    print(
        "Verified mixed Garmin acquisition, authenticated backup, both restores and paired normal offline replay."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--days", type=int, choices=(1, 7), default=7)
    arguments = parser.parse_args()
    addresses = [
        os.environ[name]
        for name in (
            "GA_TEST_DATABASE_URL",
            "GA_TEST_PYTHON_RESTORE_URL",
            "GA_TEST_NATIVE_RESTORE_URL",
        )
    ]
    source, *targets = map(make_url, addresses)
    assert len({value.database for value in (source, *targets)}) == len(addresses)
    assert all(source.set(database=target.database) == target for target in targets)
    with ExitStack() as stack:
        root = Path(stack.enter_context(TemporaryDirectory())).resolve()
        stores = [provision_engine(stack, uri) for uri in addresses]
        exercise(arguments.binary.resolve(strict=True), root, *stores, arguments.days)


if __name__ == "__main__":
    main()
