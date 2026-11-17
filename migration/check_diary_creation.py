"""Exercise the native diary creation contract on a new disposable database."""

import argparse
import subprocess
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.definitions import ensure_system_definition
from garmin_ai.events import EventInput, create_event
from garmin_ai.metric_definitions import bind_event_field, register_metric_definition

REPORT = {
    "start": "2026-10-24T23:30:00+00:00",
    "payload": {
        "type": "caffeine",
        "beverage": "Synthetic original",
        "caffeine_mg_estimate": 95,
    },
}


def run(binary):
    engine = isolated_engine()
    report = EventInput.model_validate(REPORT)
    try:
        with Session(engine) as session, session.begin():
            definition = ensure_system_definition(session, "caffeine")
            metric = register_metric_definition(
                session,
                {
                    "key": "user.synthetic-caffeine-dose",
                    "labels": {"en": "Synthetic dose"},
                    "value_kind": "physical_number",
                    "unit": "mg",
                    "dimension": "mass",
                    "aggregation": "latest",
                    "allowed_methods": ["latest"],
                    "coverage": {"kind": "all_values"},
                    "time_semantics": "point",
                    "minimum": 0,
                    "maximum": 5000,
                },
                authorized=True,
            )
            bind_event_field(
                session,
                definition.id,
                "system.caffeine.caffeine_mg_estimate",
                metric.id,
                authorized=True,
            )
            create_event(session, report, actor="api", idempotency_key="synthetic-python-event")
        subprocess.run([binary, "--ignored"], check=True)
        with Session(engine) as session, session.begin():
            event = create_event(
                session, report, actor="api", idempotency_key="synthetic-rust-event"
            )
            assert event.revision == 2
            assert event.payload["beverage"] == "Synthetic edit"
            print("Python reused the original Rust audit and returned the newer revision")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
