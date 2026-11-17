"""Compare Rust readiness with the Python application on a disposable TimescaleDB."""

import argparse
import os
import select
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.db import SCHEMA_REVISION

READY = (200, {"status": "ready"})
UNAVAILABLE = (503, {"detail": "Database unavailable or not migrated"})
MIGRATION_REQUIRED = (503, {"detail": "Database migration required"})
ERASED = (503, {"detail": "Storage disabled after erasure"})
UNKNOWN_REVISION = "000000000000"
FENCES = (72104622, 72104625)


@contextmanager
def rust_server(binary, url):
    env = {"GA_DATABASE_URL": url.render_as_string(hide_password=False)}
    with (
        tempfile.TemporaryDirectory() as home,
        subprocess.Popen(
            [binary, "serve", "--port", "0"],
            cwd=home,
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        ) as child,
    ):
        try:
            if not select.select([child.stdout], [], [], 5)[0]:
                raise RuntimeError("Rust did not print its loopback listener address")
            address = child.stdout.readline().strip().removeprefix("Listening on ")
            if not address.startswith("http://127.0.0.1:"):
                raise RuntimeError("Rust readiness fixture must listen on loopback")
            with httpx.Client(base_url=address, trust_env=False, timeout=4) as client:
                yield client
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    raise


def response(client, path="/health/ready"):
    observed = client.get(path)
    return observed.status_code, observed.json()


def expect(client, outcome):
    assert response(client) == outcome
    assert response(client, "/health/live") == (200, {"status": "alive"})


def execute(engine, statement, **parameters):
    with engine.begin() as connection:
        connection.execute(text(statement), parameters)


def confirm_fixture(engine):
    with engine.connect() as connection:
        actual = connection.scalar(text("SELECT current_database()"))
        if actual != engine.url.database or not actual.endswith("_test"):
            raise ValueError("Readiness checks need an explicit disposable _test database")
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        assert list(revision) == [SCHEMA_REVISION]
        assert connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
        )


def compare_ready_cases(engine, python, rust):
    for client in (python, rust):
        expect(client, READY)
    execute(
        engine,
        "UPDATE alembic_version SET version_num = :new WHERE version_num = :old",
        old=SCHEMA_REVISION,
        new=UNKNOWN_REVISION,
    )
    try:
        for client in (python, rust):
            expect(client, MIGRATION_REQUIRED)
    finally:
        execute(
            engine,
            "UPDATE alembic_version SET version_num = :new WHERE version_num = :old",
            old=UNKNOWN_REVISION,
            new=SCHEMA_REVISION,
        )

    execute(
        engine,
        "INSERT INTO alembic_version (version_num) VALUES (:revision)",
        revision=UNKNOWN_REVISION,
    )
    try:
        expect(rust, MIGRATION_REQUIRED)
    finally:
        execute(
            engine,
            "DELETE FROM alembic_version WHERE version_num = :revision",
            revision=UNKNOWN_REVISION,
        )

    execute(engine, "INSERT INTO app_state (key, value) VALUES ('maintenance:erased', '{}')")
    try:
        for client in (python, rust):
            expect(client, ERASED)
    finally:
        execute(engine, "DELETE FROM app_state WHERE key = 'maintenance:erased'")


def check_readiness_under_contention(engine, rust):
    for lock in FENCES:
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
            expect(rust, UNAVAILABLE)
        expect(rust, READY)
    with engine.begin() as connection:
        connection.execute(text("LOCK TABLE app_state IN ACCESS EXCLUSIVE MODE"))
        start = monotonic()
        expect(rust, UNAVAILABLE)
        assert monotonic() - start < 2.0
    expect(rust, READY)
    # Readiness transactions must release their shared locks even after a failed statement.
    with engine.begin() as connection:
        for lock in FENCES:
            assert connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(:lock)"), {"lock": lock}
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--bootstrap", action="store_true", help="Migrate a new, empty fixture")
    args = parser.parse_args()

    url = make_url(os.environ["GA_TEST_DATABASE_URL"])
    if (
        url.drivername != "postgresql+psycopg"
        or not (url.database or "").endswith("_test")
        or url.query
    ):
        parser.error(
            "GA_TEST_DATABASE_URL must name a dedicated PostgreSQL database ending in _test"
        )
    engine = isolated_engine() if args.bootstrap else create_engine(url, hide_parameters=True)
    try:
        confirm_fixture(engine)
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            settings = Settings(
                _env_file=None,
                data_dir=directory / "data",
                token_dir=directory / "tokens",
                lock_dir=directory / "locks",
                backup_dir=directory / "backup",
                integrations=[],
            )
            with (
                TestClient(create_app(settings, engine=engine)) as python,
                rust_server(args.binary.resolve(strict=True), engine.url) as rust,
            ):
                compare_ready_cases(engine, python, rust)
                check_readiness_under_contention(engine, rust)
            confirm_fixture(engine)
    finally:
        engine.dispose()
    print("Python and Rust readiness matched. Invalid revisions and storage fences stayed closed.")


if __name__ == "__main__":
    main()
