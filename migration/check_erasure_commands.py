"""Run native erase and resume with intentional interruptions on disposable storage."""

import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from check_database_export import table_values
from check_native_schema import engine_for
from check_storage_maintenance import contents, prepare
from sqlalchemy import text
from sqlalchemy.engine import make_url

ERASE = ("erase-all", "--confirm", "ERASE ALL LOCAL HEALTH DATA")


def fixture(engine, root):
    prepare(engine)
    root.mkdir()
    data, tokens, locks, backup = (root / name for name in ("data", "tokens", "locks", "backups"))
    for path in (data, tokens, backup):
        path.mkdir()
        (path / "fixture").write_text("synthetic fixture", encoding="utf-8")
    env = {
        "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "GA_DATA_DIR": str(data),
        "GA_TOKEN_DIR": str(tokens),
        "GA_LOCK_DIR": str(locks),
        "GA_BACKUP_DIR": str(backup),
    }
    return env, data, tokens, locks, backup


def run(binary, root, env, args, *, fails=False):
    result = subprocess.run(
        [binary, *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == int(fails), result.stderr
    return (result.stderr if fails else result.stdout).strip()


def wait_for_marker(process, marker, prior_inode):
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if marker.stat().st_ino != prior_inode:
                return
        except FileNotFoundError:
            pass
        time.sleep(0.005)
    raise AssertionError("Erasure process did not reach its durable file marker")


def interrupt(binary, root, env, marker, action, args=ERASE):
    prior_inode = marker.stat().st_ino if marker.exists() else None
    with subprocess.Popen(
        [binary, *args],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            wait_for_marker(process, marker, prior_inode)
            expected = action(process)
            _, error = process.communicate(timeout=15)
            assert process.returncode == expected, error
        finally:
            if process.poll() is None:
                process.kill()


def execute(engine, *statements):
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def compare(binary, reference, native, root):
    legacy = Path(sys.executable).with_name("garmin-ai")
    reference_env, _, _, _, reference_backup = fixture(reference, root / "reference")
    env, data, tokens, locks, backup = fixture(native, root / "candidate")
    expected = json.loads(run(legacy, root, reference_env, ERASE))
    expected_erased = contents(reference)
    before = table_values(native)
    assert run(binary, root, env, ("erase-all", "--confirm", "incorrect"), fails=True) == (
        "Exact erasure confirmation required"
    )
    assert not locks.exists() and table_values(native) == before
    with native.begin() as active:
        active.execute(text("SELECT pg_advisory_xact_lock(72104620)"))
        assert "Stop the runtime" in run(binary, root, env, ERASE, fails=True)
    assert not (locks / "erased").exists()
    local_export = backup / "logical.jsonl.gz"
    assert len(json.loads(run(binary, root, env, ("export", local_export)))) == 30

    execute(
        native,
        """CREATE FUNCTION wait_for_erasure_fixture() RETURNS trigger AS $$
           BEGIN PERFORM pg_sleep(2); RETURN NULL; END; $$ LANGUAGE plpgsql""",
        """CREATE TRIGGER wait_for_erasure_fixture BEFORE TRUNCATE ON people
           FOR EACH STATEMENT EXECUTE FUNCTION wait_for_erasure_fixture()""",
    )
    marker = locks / "erasing"

    def kill(process):
        process.kill()
        return -9

    interrupt(binary, root, env, marker, kill)
    assert marker.is_file() and (locks / "erased").is_file()
    assert data.is_dir() and tokens.is_dir() and table_values(native) == before
    assert "Complete the interrupted erasure" in run(
        binary, root, env, ("resume-storage",), fails=True
    )

    late_redirect = tokens / "late-fixture-redirect"

    def introduce_redirect(_):
        late_redirect.symlink_to(backup)
        return 1

    interrupt(binary, root, env, marker, introduce_redirect)
    assert marker.is_file() and (locks / "erased").is_file()
    assert not data.exists() and tokens.is_dir() and late_redirect.is_symlink()
    assert contents(native) == expected_erased
    assert "Complete the interrupted erasure" in run(
        binary, root, env, ("resume-storage",), fails=True
    )
    run(binary, root, env, ("restore-db", local_export), fails=True)
    assert contents(native) == expected_erased and not (locks / "activating").exists()
    late_redirect.unlink()
    execute(
        native,
        "DROP TRIGGER wait_for_erasure_fixture ON people",
        "DROP FUNCTION wait_for_erasure_fixture()",
    )
    assert json.loads(run(binary, root, env, ERASE)) == expected
    assert json.loads(run(binary, root, env, ERASE)) == expected
    assert not marker.exists() and (locks / "erased").is_file()
    assert not data.exists() and not tokens.exists() and contents(native) == expected_erased

    execute(
        native,
        """CREATE FUNCTION reject_resume_fixture() RETURNS trigger AS $$
           BEGIN RAISE EXCEPTION 'synthetic resume failure'; END; $$ LANGUAGE plpgsql""",
        """CREATE TRIGGER reject_resume_fixture BEFORE DELETE ON app_state
           FOR EACH STATEMENT EXECUTE FUNCTION reject_resume_fixture()""",
    )
    assert "could not be committed" in run(binary, root, env, ("resume-storage",), fails=True)
    file_only = {key: value for key, value in env.items() if key != "GA_DATABASE_URL"}
    activation = locks / "activating"

    def assert_activation_is_fenced():
        refused = run(legacy, root, file_only, ("probe",), fails=True)
        assert refused == "Operation failed (ValueError). No sensitive details logged."
        assert not data.exists() and not tokens.exists()
        assert activation.is_file() and (locks / "erased").is_file()
        assert contents(native) == expected_erased

    assert_activation_is_fenced()
    execute(
        native,
        """CREATE OR REPLACE FUNCTION reject_resume_fixture() RETURNS trigger AS $$
           BEGIN PERFORM pg_sleep(2); RETURN NULL; END; $$ LANGUAGE plpgsql""",
    )
    interrupt(binary, root, env, activation, kill, args=("resume-storage",))
    assert_activation_is_fenced()
    execute(
        native,
        "DROP TRIGGER reject_resume_fixture ON app_state",
        "DROP FUNCTION reject_resume_fixture()",
    )
    assert run(binary, root, env, ("resume-storage",)) == run(
        legacy, root, reference_env, ("resume-storage",)
    )
    assert run(binary, root, env, ("resume-storage",)) == "Storage re-enabled."
    assert all(not (locks / name).exists() for name in ("erased", "activating", "erasing"))
    assert contents(native) == contents(reference)
    assert (backup / "fixture").read_bytes() == (reference_backup / "fixture").read_bytes()
    print(
        "Erasure and resume matched Python after restart, partial file removal, and blocked activation"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("name_prefix")
    args = parser.parse_args()
    opening = make_url(os.environ["GA_TEST_DATABASE_URL"])
    assert opening.database and opening.database.endswith("_test")
    with ExitStack() as stack:
        reference, native = [
            engine_for(stack, opening.set(database=f"{args.name_prefix}_{kind}_test"))
            for kind in ("py", "rs")
        ]
        with TemporaryDirectory() as root:
            compare(args.binary.resolve(strict=True), reference, native, Path(root).resolve())


if __name__ == "__main__":
    main()
