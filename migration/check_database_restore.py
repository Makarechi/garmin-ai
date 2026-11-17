"""Recover the current pinned Python snapshot into a disposable native destination."""

import argparse
import gzip
import math
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from capture_reference import isolated_engine
from check_database_export import seed, table_values
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.config import Settings
from garmin_ai.definitions import ensure_system_definitions
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import AppState
from garmin_ai.operations import export_database
from garmin_ai.scenario_packs import ensure_scenario_packs


def run(binary):
    target = os.environ["GA_TEST_RESTORE_DATABASE_URL"]
    source = make_url(os.environ["GA_TEST_DATABASE_URL"])
    same_server = make_url(target)
    assert source.set(database=same_server.database) == same_server
    assert source.database != same_server.database
    engine = isolated_engine()
    try:
        with patch.dict(os.environ, {"GA_TEST_DATABASE_URL": target}):
            restored = isolated_engine()
        try:
            seed(engine)
            with Session(restored) as session, session.begin():
                ensure_system_definitions(session)
                ensure_system_metric_definitions(session)
                ensure_scenario_packs(session, legacy_install=False)
                session.add(AppState(key="bootstrap:restore-test", value={"test": True}))
            with TemporaryDirectory() as directory:
                root = Path(directory)
                reference = root / "reference.jsonl.gz"
                input_file = root / "input.jsonl"
                counts = export_database(engine, reference, settings=Settings(_env_file=None))
                with gzip.open(reference, "rb") as compressed, input_file.open("wb") as output:
                    shutil.copyfileobj(compressed, output, length=65536)
                subprocess.run(
                    [binary, "--ignored", "--nocapture"],
                    env={
                        **os.environ,
                        "GA_TEST_DATABASE_URL": target,
                        "GA_TEST_RESTORE_INPUT": str(input_file),
                    },
                    check=True,
                )
                original = table_values(engine)
                actual = table_values(restored)
                assert actual.keys() == original.keys() == counts.keys()
                for table in original:
                    assert actual[table] == original[table], table
                assert math.copysign(1.0, actual["health_days"][0]["deep_seconds"]) == -1.0
                with restored.begin() as connection:
                    previous = max(row["id"] for row in actual["audit_log"])
                    statement = text(
                        "INSERT INTO audit_log(event_id,action,actor) "
                        "SELECT id,'synthetic','api' FROM events RETURNING id"
                    )
                    assert connection.scalar(statement) > previous
        finally:
            restored.dispose()
    finally:
        engine.dispose()
    print(
        f"Rust restored every typed value from {len(counts)} tables and {sum(counts.values())} rows"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
