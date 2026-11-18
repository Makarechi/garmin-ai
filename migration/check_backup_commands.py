"""Exercise both complete backup recovery directions with two disposable readers."""

import argparse
import base64
import hashlib
import json
import math
import os
import random
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from capture_reference import isolated_engine
from check_database_export import seed, table_values
from sqlalchemy import text
from sqlalchemy.engine import make_url

from garmin_ai import operations
from garmin_ai.config import Settings

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def cli_environment(settings):
    return {
        "GA_DATABASE_URL": settings.database_url.get_secret_value(),
        "GA_DATA_DIR": str(settings.data_dir),
        "GA_TOKEN_DIR": str(settings.token_dir),
        "GA_LOCK_DIR": str(settings.lock_dir),
        "GA_BACKUP_KEY": KEY,
        "GA_BACKUP_DIR": str(settings.backup_dir),
    }


def run_cli(binary, cwd, settings, *args, succeeds=True):
    result = subprocess.run(
        [binary, *map(str, args)],
        cwd=cwd,
        env=cli_environment(settings),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if not succeeds:
        assert result.returncode == 1 and not result.stdout and result.stderr, args[0]
        return None
    assert result.returncode == 0, (args[0], result.stderr)
    assert not result.stderr, (args[0], result.stderr)
    return result.stdout.strip()


def settings(root, engine):
    return Settings(
        _env_file=None,
        data_dir=root / "data",
        token_dir=root / "tokens",
        lock_dir=root / "locks",
        backup_dir=root / "offhost",
        backup_key=KEY,
        database_url=engine.url.render_as_string(hide_password=False),
    )


def capture(engine, config):
    seed(engine)
    contents = b"Synthetic backup file\n" * 50_000
    digest = hashlib.sha256(contents).hexdigest()
    key = f"{digest[:2]}/{digest}.json"
    raw = config.data_dir / "raw" / key
    raw.parent.mkdir(parents=True)
    raw.write_bytes(contents)
    with engine.begin() as connection:
        connection.execute(text("UPDATE source_payloads SET archive_key=:key"), {"key": key})
    config.token_dir.mkdir()
    (config.token_dir / "synthetic").write_bytes(b"only a synthetic fixture")
    (config.data_dir / "coverage-report.json").write_bytes(b'{"source":"fixture"}')
    return raw, contents


def check_recovered_files(recovery, source):
    for directory, origin in (("raw", source.data_dir / "raw"), ("tokens", source.token_dir)):
        for path in origin.rglob("*"):
            if path.is_file():
                target = recovery / directory / path.relative_to(origin)
                assert target.read_bytes() == path.read_bytes()
                assert target.stat().st_mode & 0o777 == 0o600
    assert (recovery / "coverage-report.json").read_bytes() == (
        source.data_dir / "coverage-report.json"
    ).read_bytes()


def check_rows(engine, expected):
    actual = table_values(engine)
    assert actual.keys() == expected.keys()
    for table in expected:
        assert actual[table] == expected[table], table
    assert math.copysign(1.0, actual["health_days"][0]["deep_seconds"]) == -1.0


def provision_engine(stack, address):
    with patch.dict(os.environ, {"GA_TEST_DATABASE_URL": address}):
        engine = isolated_engine()
    stack.callback(engine.dispose)
    return engine


def interrupt_at_staging(binary, cwd, config, workspace):
    extra = config.data_dir / "raw/large-synthetic.bin"
    rng = random.Random(710)
    with extra.open("xb") as stream:
        for _ in range(768):
            stream.write(rng.randbytes(65536))
    destination = config.backup_dir / "interrupted.enc"
    try:
        with subprocess.Popen(
            [binary, "backup", destination],
            cwd=cwd,
            env=cli_environment(config),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and process.poll() is None:
                    if any(config.backup_dir.glob(".rust-backup-*")) and workspace.exists():
                        process.kill()
                        break
                    time.sleep(0.002)
                assert process.wait(timeout=5) == -9
            finally:
                if process.poll() is None:
                    process.kill()
    finally:
        extra.unlink()
    assert not destination.exists()
    assert (workspace / "database.jsonl.gz").is_file()


def verify(binary, root, source, native_target, python_target, export_target):
    origin = settings(root / "origin", source)
    target = settings(root / "target", native_target)
    standalone = settings(root / "standalone", export_target)
    raw, contents = capture(source, origin)

    python_archive = origin.backup_dir / "python.enc"
    expected_counts = operations.create_backup(source, origin, python_archive)
    expected = table_values(source)
    workspace = origin.data_dir / "backup-work/.garmin-ai-plaintext/active"
    workspace.mkdir()
    (workspace / "database.jsonl.gz").write_bytes(b"synthetic interrupted workspace")
    rust_archive = origin.backup_dir / "native.enc"
    assert json.loads(run_cli(binary, root, origin, "backup", rust_archive)) == expected_counts
    assert not workspace.exists()
    archived = hashlib.sha256(rust_archive.read_bytes()).digest()

    interrupt_at_staging(binary, root, origin, workspace)
    assert hashlib.sha256(rust_archive.read_bytes()).digest() == archived
    restarted = origin.backup_dir / "restarted.enc"
    assert json.loads(run_cli(binary, root, origin, "backup", restarted)) == expected_counts
    assert not workspace.exists()

    raw.write_bytes(b"broken fixture")
    run_cli(binary, root, origin, "backup", origin.backup_dir / "broken.enc", succeeds=False)
    assert not (origin.backup_dir / "broken.enc").exists()
    raw.write_bytes(contents)
    run_cli(binary, root, origin, "backup", rust_archive, succeeds=False)
    assert hashlib.sha256(rust_archive.read_bytes()).digest() == archived
    assert not workspace.exists()

    legacy_recovery = root / "python-recovery"
    operations.unpack_backup(origin, rust_archive, legacy_recovery)
    check_recovered_files(legacy_recovery, origin)
    assert (
        operations.restore_database(python_target, legacy_recovery / "database.jsonl.gz")
        == expected_counts
    )
    check_rows(python_target, expected)

    native_recovery = root / "native-recovery"
    assert (
        run_cli(binary, root, origin, "unpack-backup", python_archive, native_recovery)
        == "Backup unpacked."
    )
    check_recovered_files(native_recovery, origin)
    run_cli(binary, root, origin, "unpack-backup", python_archive, native_recovery, succeeds=False)

    target.lock_dir.mkdir(parents=True)
    erased = target.lock_dir / "erased"
    erased.write_bytes(b"Test erase fence\n")
    with native_target.begin() as connection:
        connection.execute(
            text("INSERT INTO app_state(key,value) VALUES ('maintenance:erased','{}')")
        )
    prior = table_values(native_target)
    wrong = root / "bad-export.jsonl.gz"
    logical = native_recovery / "database.jsonl.gz"
    broken = bytearray(logical.read_bytes())
    broken[-8] ^= 1
    wrong.write_bytes(broken)
    run_cli(binary, root, target, "restore-db", wrong, succeeds=False)
    assert table_values(native_target) == prior
    assert erased.read_bytes() == b"Test erase fence\n"
    assert not (target.lock_dir / "activating").exists()

    with native_target.begin() as connection:
        connection.execute(
            text("""
                CREATE FUNCTION reject_fixture_commit() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'synthetic commit failure'; END;
                $$ LANGUAGE plpgsql
                """)
        )
        connection.execute(
            text("""
                CREATE CONSTRAINT TRIGGER reject_fixture_commit AFTER INSERT ON events
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
                EXECUTE FUNCTION reject_fixture_commit()
                """)
        )
    run_cli(binary, root, target, "restore-db", logical, succeeds=False)
    assert table_values(native_target) == prior
    assert erased.read_bytes() == b"Test erase fence\n"
    assert (target.lock_dir / "activating").is_file()
    with native_target.begin() as connection:
        connection.execute(text("DROP TRIGGER reject_fixture_commit ON events"))
        connection.execute(text("DROP FUNCTION reject_fixture_commit()"))
    assert json.loads(run_cli(binary, root, target, "restore-db", logical)) == expected_counts
    assert not erased.exists() and not (target.lock_dir / "activating").exists()
    check_rows(native_target, expected)
    run_cli(binary, root, target, "restore-db", logical, succeeds=False)
    check_rows(native_target, expected)

    export = root / "standalone.jsonl.gz"
    assert json.loads(run_cli(binary, root, origin, "export", export)) == expected_counts
    assert export.stat().st_mode & 0o777 == 0o600
    assert json.loads(run_cli(binary, root, standalone, "restore-db", export)) == expected_counts
    check_rows(export_target, expected)
    print(
        f"Both complete backup directions and native export recovered {len(expected_counts)} tables"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    source = os.environ["GA_TEST_DATABASE_URL"]
    addresses = [
        source,
        os.environ["GA_TEST_NATIVE_RESTORE_URL"],
        os.environ["GA_TEST_PYTHON_RESTORE_URL"],
        os.environ["GA_TEST_EXPORT_RESTORE_URL"],
    ]
    source_url = make_url(source)
    parsed = list(map(make_url, addresses))
    assert len({url.database for url in parsed}) == len(parsed)
    assert all(source_url.set(database=url.database) == url for url in parsed[1:])
    with ExitStack() as stack:
        root = Path(stack.enter_context(TemporaryDirectory())).resolve()
        engines = [provision_engine(stack, url) for url in addresses]
        verify(args.binary.resolve(strict=True), root, *engines)


if __name__ == "__main__":
    main()
