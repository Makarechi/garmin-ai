"""Compare Python and Rust projections of a synthetic Garmin activity."""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import ingest
from garmin_ai.models import Activity, AppState, SourcePayload

PYTHON_ID = "7000000001"
RUST_ID = "7000000002"
IGNORED = {"id", "updated_at"}


def projection(session, identity):
    activity = session.get(Activity, identity)
    state = session.get(AppState, f"activity-version:{identity}").value
    sources = {
        field: session.get(SourcePayload, source_id).endpoint
        for field, source_id in state["owners"].items()
    }
    return (
        {
            column.name: getattr(activity, column.name)
            for column in activity.__table__.columns
            if column.name not in IGNORED
        },
        {**state, "owners": sources},
    )


def run(binary, fixtures):
    engine = isolated_engine()
    try:
        with TemporaryDirectory() as archive, Session(engine) as session, session.begin():
            for step in json.loads(fixtures.read_text(encoding="utf-8")):
                page, document = step["page"], step["summary"]
                document["activityId"] = PYTHON_ID
                result = ingest(
                    session,
                    LocalArchive(Path(archive)),
                    "activities" if page else "activity",
                    page or PYTHON_ID,
                    [document] if page else document,
                    "UTC",
                    fetched_at=datetime.fromtimestamp(1_790_000_000 + step["second"], UTC),
                )
                assert result["status"] == step["status"]
                if page == "page:0":
                    state = session.get(AppState, f"activity-version:{PYTHON_ID}")
                    state.value = {"requested_at": state.value["requested_at"]}
                    session.flush()
        subprocess.run([binary, "--ignored"], check=True)
        with Session(engine) as session, session.begin():
            assert projection(session, PYTHON_ID) == projection(session, RUST_ID)
            print("Rust and pinned Python agree on activity fields and stored ownership")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("fixtures", type=Path)
    args = parser.parse_args()
    run(args.binary.resolve(strict=True), args.fixtures.resolve(strict=True))
