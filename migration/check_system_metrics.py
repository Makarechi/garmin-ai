"""Compare the native metric registry with pinned Python on a disposable database."""

import argparse
import subprocess
from pathlib import Path

from capture_reference import isolated_engine
from generate_system_metrics import export
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import MetricDefinition, MetricDefinitionVersion


def run(binary, seed):
    engine = isolated_engine()
    try:
        if seed:
            with Session(engine) as session, session.begin():
                assert len(ensure_system_metric_definitions(session)) == 11
        subprocess.run([binary, "--ignored"], check=True)
        with Session(engine) as session, session.begin():
            exported = export()
            versions = session.execute(
                select(MetricDefinition.key, MetricDefinitionVersion)
                .join(MetricDefinitionVersion)
                .where(MetricDefinitionVersion.version == MetricDefinition.current_version)
            ).all()
            assert len(versions) == len(exported)
            for key, stored in versions:
                record = exported[key.removeprefix("system.")]
                assert stored.schema_hash == record["hash"]
                for field, value in record["contract"].items():
                    if field == "key":
                        assert key == value
                    else:
                        assert (
                            getattr(stored, "coverage_policy" if field == "coverage" else field)
                            == value
                        )
            assert {key: row.id for key, row in versions} == {
                f"system.{key}": version.id
                for key, version in ensure_system_metric_definitions(session).items()
            }
            print("Pinned Python verified all 11 current Rust metric contracts and identifiers")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--python-seeded", action="store_true")
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.python_seeded)
