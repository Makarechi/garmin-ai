"""Compare activity part recovery from a verified archive with pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID

from capture_reference import isolated_engine
from check_replay_calendar import ACCOUNT, AT, CURRENT_ZONE, ORIGINAL_ZONE
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from garmin_ai import replay
from garmin_ai.archive import LocalArchive
from garmin_ai.ingest import ingest
from garmin_ai.models import Activity, ActivityPart, AppState, SourcePayload


def seed(engine, archive):
    with Session(engine) as session, session.begin():
        session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))
        for identity in ["1", "2", "3", "4", "5"]:
            session.add(
                Activity(id=identity, kind="running", start=AT, end=AT, timezone=ORIGINAL_ZONE)
            )
        session.flush()

        def write(endpoint, key, payload, sequence):
            result = ingest(
                session,
                archive,
                endpoint,
                key,
                payload,
                ORIGINAL_ZONE,
                fetched_at=AT + timedelta(minutes=sequence),
            )
            assert result["status"] in {"archived", "empty"}
            return session.get(SourcePayload, UUID(result["source_ref"]))

        active = write("activity_details", "1", [{"a": 1}, {"b": 2}], 1)
        write("activity_weather", "1", {"separate": 90}, 2)
        active.payload = {"incorrect_database_copy": True}
        session.execute(
            update(ActivityPart)
            .where(ActivityPart.activity_id == "1", ActivityPart.kind == "activity_details")
            .values(payload={"obsolete": True})
        )
        prior = write("activity_splits", "2", [{"first": 1}], 3)
        empty = write("activity_splits", "2", [], 4)
        session.execute(
            update(ActivityPart)
            .where(ActivityPart.activity_id == "2")
            .values(payload={"obsolete": "retained"})
        )
        replaced = write("activity_zones", "3", {"zone": "older"}, 5)
        write("activity_zones", "3", {"zone": "newer"}, 6)
        corrupt = write("activity_typed_splits", "4", {"kind": "damaged"}, 7)
        path = archive.root / corrupt.archive_key
        path.write_bytes(path.read_bytes().replace(b"damaged", b"altered"))
        forced = write("activity_weather", "5", {"weather": "recovered"}, 8)
        session.execute(
            update(ActivityPart)
            .where(ActivityPart.activity_id == "5")
            .values(payload={"weather": "obsolete"})
        )
        missing = write("activity_weather", "6", {"raw_only": True}, 9)
        refs = {
            "active": active,
            "archived_only": missing,
            "corrupt": corrupt,
            "empty": empty,
            "forced": forced,
            "retained": prior,
            "superseded": replaced,
        }
        for row in refs.values():
            row.parser_version = 8
        return {name: str(raw.id) for name, raw in refs.items()}


def snapshot(engine, refs):
    with Session(engine) as session:
        raws = session.scalars(select(SourcePayload)).all()
        identities = {str(row.id): row.payload_hash for row in raws}

        def stable(value):
            if isinstance(value, str):
                return identities.get(value, value)
            if isinstance(value, dict):
                return {key: stable(item) for key, item in value.items()}
            return value

        parts = session.scalars(
            select(ActivityPart).order_by(
                ActivityPart.activity_id, ActivityPart.kind, ActivityPart.sequence
            )
        ).all()
        state = session.scalars(select(AppState).order_by(AppState.key)).all()
        return {
            "parts": [(row.activity_id, row.kind, row.sequence, row.payload) for row in parts],
            "raws": {
                row.payload_hash: [row.status, row.parser_version, row.payload] for row in raws
            },
            "owners": {
                row.key: stable(row.value)
                for row in state
                if row.key.startswith("activity-parts-owner:")
            },
            "watermarks": {
                row.key: stable(
                    {
                        "source_ref": row.value.get("source_ref"),
                        "requested_at": row.value.get("requested_at"),
                        "status": row.value.get("status"),
                        "latest_status": row.value.get("latest_attempt", {}).get("status"),
                        "latest_ref": row.value.get("latest_attempt", {}).get("source_ref"),
                    }
                )
                for row in state
                if row.key.startswith("ingest:")
            },
            "status": {
                name: session.get(AppState, f"replay:{ref}:9").value["status"]
                for name, ref in refs.items()
            },
        }


def run(binary):
    primary_url = os.environ["GA_TEST_DATABASE_URL"]
    native_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    assert primary_url != native_url
    python = isolated_engine()
    try:
        os.environ["GA_TEST_DATABASE_URL"] = native_url
        native = isolated_engine()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = primary_url
    try:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive_python = LocalArchive(root / "python")
            archive_native = LocalArchive(root / "native")
            py_refs = seed(python, archive_python)
            native_refs = seed(native, archive_native)
            for name, identity in py_refs.items():
                try:
                    replay.run_replay(
                        python,
                        archive_python,
                        SimpleNamespace(timezone=CURRENT_ZONE),
                        {"raw_ref": identity, "account": ACCOUNT, "target_version": 9},
                    )
                except RuntimeError:
                    assert name == "corrupt"
            expected = snapshot(python, py_refs)
            cases = {
                name: {"ref": ref, "status": expected["status"][name]}
                for name, ref in native_refs.items()
            }
            path = root / "activity-replay.json"
            path.write_text(json.dumps(cases), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored"],
                check=True,
                env={
                    **os.environ,
                    "GA_TEST_DATABASE_URL": native_url,
                    "GA_TEST_ACTIVITY_REPLAY": str(path),
                    "GA_TEST_ACTIVITY_REPLAY_ARCHIVE": str(archive_native.root),
                },
            )
            actual = snapshot(native, native_refs)
            assert actual == expected, {
                key: (actual[key], expected[key])
                for key in expected
                if actual[key] != expected[key]
            }
        print(f"Matched {len(py_refs)} activity document replay cases with pinned Python")
    finally:
        python.dispose()
        native.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
