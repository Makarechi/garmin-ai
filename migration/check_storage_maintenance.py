"""Compare explicit erasure and resume with Python on two disposable databases."""

import argparse
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from check_database_export import seed, table_values
from check_native_schema import CURRENT, engine_for, upgrade
from sqlalchemy import text
from sqlalchemy.engine import make_url


def prepare(engine):
    upgrade(engine, CURRENT)
    seed(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO app_state(key,value) VALUES ('test:erase-seed','{}')"))
        connection.execute(text("CREATE TABLE fixture_unrelated(id integer PRIMARY KEY)"))
        connection.execute(text("INSERT INTO fixture_unrelated VALUES (17)"))


def contents(engine):
    result = table_values(engine)
    result["app_state"] = [(row["key"], row["value"]) for row in result["app_state"]]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM fixture_unrelated")) == 17
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT
        for sequence in ("audit_log_id_seq", "telegram_updates_id_seq"):
            assert connection.execute(
                text(f"SELECT last_value,is_called FROM {sequence}")
            ).one() == (
                1,
                False,
            )
    return result


def native(binary, engine, case):
    subprocess.run(
        [binary, "--ignored", "--exact", case, "--nocapture"],
        env={"GA_TEST_DATABASE_URL": engine.url.render_as_string(hide_password=False)},
        check=True,
        timeout=25,
    )


def compare(binary, reference, candidate, root):
    for engine in (reference, candidate):
        prepare(engine)
    files = {
        key: root / directory
        for key, directory in (
            ("GA_DATA_DIR", "health-data"),
            ("GA_TOKEN_DIR", "integration-data"),
            ("GA_LOCK_DIR", "locks"),
        )
    }
    for directory in (files["GA_DATA_DIR"], files["GA_TOKEN_DIR"]):
        directory.mkdir()
        (directory / "local-fixture").write_text("synthetic", encoding="utf-8")
    outside = root / "separate-backup"
    outside.write_text("independent", encoding="utf-8")
    legacy = Path(sys.executable).with_name("garmin-ai")
    env = {**files, "GA_DATABASE_URL": reference.url.render_as_string(hide_password=False)}
    subprocess.run(
        [legacy, "erase-all", "--confirm", "ERASE ALL LOCAL HEALTH DATA"],
        cwd=root,
        env=env,
        check=True,
        timeout=25,
    )
    assert not files["GA_DATA_DIR"].exists() and not files["GA_TOKEN_DIR"].exists()
    assert (files["GA_LOCK_DIR"] / "erased").is_file()
    native(binary, candidate, "erasure_fences_writers_and_preserves_unrelated_tables")
    expected = contents(reference)
    assert contents(candidate) == expected
    assert len(expected) == 30
    assert [(table, rows) for table, rows in expected.items() if rows] == [
        ("app_state", [("maintenance:erased", {"disabled": True})])
    ]
    subprocess.run([legacy, "resume-storage"], cwd=root, env=env, check=True, timeout=25)
    assert not (files["GA_LOCK_DIR"] / "erased").exists()
    native(binary, candidate, "resume_removes_only_the_database_fence")
    assert contents(reference) == contents(candidate)
    assert all(not rows for rows in contents(candidate).values())
    assert outside.read_text(encoding="utf-8") == "independent"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("name_prefix")
    args = parser.parse_args()
    opening = make_url(os.environ["GA_TEST_DATABASE_URL"])
    assert opening.database and opening.database.endswith("_test")
    with ExitStack() as stack:
        reference, native_engine = [
            engine_for(stack, opening.set(database=f"{args.name_prefix}_{kind}_test"))
            for kind in ("py", "rs")
        ]
        with TemporaryDirectory() as root:
            compare(
                args.binary.resolve(strict=True), reference, native_engine, Path(root).resolve()
            )
    print("Matched all thirty database tables, both sequences and the erasure fence")


if __name__ == "__main__":
    main()
