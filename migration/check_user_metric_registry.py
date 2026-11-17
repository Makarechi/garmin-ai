"""Verify Rust and pinned Python read the same user metric versions."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from capture_reference import isolated_engine
from check_user_metric_contracts import BASE
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.metric_definitions import MetricSpec, metric_hash, register_metric_definition
from garmin_ai.models import MetricDefinition, MetricDefinitionVersion


def verify(binary):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            original = register_metric_definition(session, BASE, authorized=True).id
        subprocess.run(
            [binary, "--ignored"],
            check=True,
            env={**os.environ, "GA_TEST_PYTHON_USER_METRIC": json.dumps(BASE)},
        )
        with Session(engine) as session, session.begin():
            updated = register_metric_definition(session, BASE | {"maximum": 201}, authorized=True)
            assert updated.version == 2
            assert session.get(MetricDefinitionVersion, original).maximum == 200
            definition = session.scalar(
                select(MetricDefinition).where(MetricDefinition.key == BASE["key"])
            )
            assert definition.status == "active"
            assert definition.current_version == 2
            assert updated.schema_hash == metric_hash(
                MetricSpec.model_validate(BASE | {"maximum": 201.0})
            )
            print("Pinned Python reused the metric version written by Rust")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    verify(parser.parse_args().binary.resolve(strict=True))
