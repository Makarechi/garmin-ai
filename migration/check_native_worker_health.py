"""Exercise the native worker's health probe on a disposable database."""

import argparse
import json
import selectors
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from sqlalchemy import text


def wait_ready(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=5)
    assert process.stdout.readline().strip() == "Garmin AI worker ready"


def read_heartbeat(engine):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT value FROM app_state WHERE key='runtime:heartbeat'")
        ).scalar_one()


def run(binary):
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw").mkdir(mode=0o700)
            environment = {
                "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                "GA_DATA_DIR": directory,
                "GA_TIMEZONE": "UTC",
            }

            def healthy():
                result = subprocess.run(
                    [binary, "healthcheck", "--worker"],
                    env=environment,
                    cwd=directory,
                    capture_output=True,
                    timeout=3,
                )
                assert not result.stdout and not result.stderr
                return result.returncode == 0

            assert not healthy()
            with engine.begin() as connection:
                connection.execute(
                    text("""
                    INSERT INTO app_state(key,value)
                    VALUES ('runtime:heartbeat',jsonb_build_object('at',clock_timestamp()))
                    """)
                )
            with engine.begin() as connection:
                connection.execute(text("SELECT pg_advisory_xact_lock(72104620)"))
                assert not healthy()

            with subprocess.Popen(
                [binary, "worker"],
                env=environment,
                cwd=directory,
                stdout=subprocess.PIPE,
                text=True,
            ) as process:
                try:
                    wait_ready(process)
                    assert healthy()
                    initial = read_heartbeat(engine)
                    assert initial["native_pid"] > 0
                    for interval in ("-181 seconds", "10 seconds"):
                        with engine.begin() as connection:
                            connection.execute(
                                text("""
                                UPDATE app_state SET value=value || jsonb_build_object(
                                    'at', clock_timestamp() + CAST(:interval AS interval))
                                WHERE key='runtime:heartbeat'
                                """),
                                {"interval": interval},
                            )
                        assert not healthy()
                    with engine.begin() as connection:
                        connection.execute(
                            text("""
                            UPDATE app_state SET value=CAST(:pulse AS jsonb)
                            WHERE key='runtime:heartbeat'
                            """),
                            {"pulse": json.dumps(initial)},
                        )
                    assert healthy()

                    deadline = time.monotonic() + 36
                    while time.monotonic() < deadline:
                        pulse = read_heartbeat(engine)
                        if pulse["at"] > initial["at"]:
                            assert pulse["native_pid"] == initial["native_pid"] and healthy()
                            break
                        time.sleep(0.25)
                    else:
                        raise AssertionError("The worker did not renew its heartbeat")

                    with engine.begin() as connection:
                        terminated = connection.execute(
                            text("SELECT pg_terminate_backend(:pid)"),
                            {"pid": initial["native_pid"]},
                        ).scalar_one()
                        assert terminated
                    assert not healthy()
                    assert process.wait(timeout=4) != 0
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)

            with subprocess.Popen(
                [binary, "worker"],
                env=environment,
                cwd=directory,
                stdout=subprocess.PIPE,
                text=True,
            ) as replacement:
                try:
                    wait_ready(replacement)
                    assert healthy()
                    assert read_heartbeat(engine)["native_pid"] != initial["native_pid"]
                finally:
                    if replacement.poll() is None:
                        replacement.terminate()
                        assert replacement.wait(timeout=5) == 0
            assert not healthy()
    finally:
        engine.dispose()
    print("Verified native worker health, renewal, session loss, restart and shutdown")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
