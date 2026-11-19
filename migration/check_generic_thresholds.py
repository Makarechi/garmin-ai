"""Compare complete native threshold reports with the frozen application."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from check_generic_aggregate_history import capture
from check_generic_observation_history import seed as seed_observations
from generic_aggregate_fixtures import configure_phase
from generic_threshold_fixtures import cases, seed
from sqlalchemy import text


def compare(binary, engine, fixture, requests, label):
    values = capture(engine, requests)
    fixture.write_text(json.dumps(values, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    subprocess.run(
        [binary, "--ignored", "--exact", "compares_generic_threshold_history"],
        check=True,
        env={**os.environ, "GA_TEST_GENERIC_THRESHOLDS": str(fixture)},
    )
    print("Matched generic thresholds", label, "cases", len(values), flush=True)


def run(binary):
    engine = isolated_engine()
    try:
        owner = seed_observations(engine)
        sources = seed(engine, owner)
        requests = cases(owner, sources)
        with TemporaryDirectory() as directory:
            fixture = Path(directory) / "thresholds.json"
            compare(binary, engine, fixture, requests, "full intervals and local clock hours")
            smaller = [case for case in requests if "random-" not in case["name"]]
            for phase in [
                "no_grant",
                "schema",
                "facts",
                "health_only",
                "diary_only",
                "activity_off",
                "replay",
            ]:
                configure_phase(engine, phase)
                compare(binary, engine, fixture, smaller, phase)
            configure_phase(engine, "facts")
            for zone in ["America/New_York", "Asia/Kathmandu"]:
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                    connection.execute(
                        text(
                            "ALTER DATABASE " + engine.url.database + " SET TimeZone='" + zone + "'"
                        )
                    )
                engine.dispose()
                compare(binary, engine, fixture, smaller, "database timezone " + zone)
    finally:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text("ALTER DATABASE " + engine.url.database + " RESET TimeZone"))
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    run(parser.parse_args().binary)
