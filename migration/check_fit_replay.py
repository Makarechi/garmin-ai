"""Compare native offline FIT replay against pinned Python with synthetic archives."""

import argparse
import json
import os
import subprocess
import zipfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID

from capture_reference import isolated_engine
from check_replay_calendar import ACCOUNT, AT, CURRENT_ZONE
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from garmin_ai import replay
from garmin_ai.archive import LocalArchive
from garmin_ai.fit import store_fit
from garmin_ai.models import Activity, ActivityPart, AppState, SourcePayload

FIRST = bytes.fromhex("0e2034080b0000002e464954fbdc4000001400010301020048b0e5")
NEXT = bytes.fromhex("0e2034080b0000002e464954fbdc40000014000103010200497125")
BAD_CRC = NEXT[:-1] + bytes([NEXT[-1] ^ 1])


def export_zip():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in [("b.FIT", NEXT), ("a.fit", FIRST), ("ignored.txt", b"synthetic")]:
            bundle.writestr(
                zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)),
                data,
                compress_type=zipfile.ZIP_DEFLATED,
            )
    return buffer.getvalue()


def seed(engine, archive):
    with Session(engine) as session, session.begin():
        session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))
        for number in range(1, 9):
            session.add(Activity(id=str(number), start=AT, end=AT, kind="running", timezone="UTC"))
        session.flush()

        def save(key, body, sequence):
            result = store_fit(session, archive, key, body, AT + timedelta(minutes=sequence))
            return session.get(SourcePayload, UUID(result["source_ref"]))

        retained = save("1", FIRST, 1)
        later_failure = save("1", BAD_CRC, 2)
        assert later_failure.status == "error"
        session.execute(
            update(ActivityPart)
            .where(ActivityPart.activity_id == "1", ActivityPart.kind == "fit_record")
            .values(payload={"heart_rate": 70})
        )
        session.add(
            ActivityPart(
                activity_id="1", kind="activity_weather", sequence=0, payload={"kept": True}
            )
        )
        bad = save("2", BAD_CRC, 2)
        assert bad.status == "error"
        save("3", FIRST, 0)
        empty = save("3", b"", 3)
        superseded = save("4", FIRST, 2)
        save("4", NEXT, 4)
        clean = save("5", FIRST, 5)
        bad_archive = save("6", FIRST + FIRST, 6)
        source_bytes = archive.root / bad_archive.archive_key
        source_bytes.write_bytes(source_bytes.read_bytes()[:-1] + b"\x00")
        forced = save("7", NEXT, 7)
        session.execute(
            update(ActivityPart)
            .where(ActivityPart.activity_id == "7")
            .values(payload={"heart_rate": 70})
        )
        zipped = save("8", export_zip(), 8)
        session.execute(
            update(ActivityPart)
            .where(ActivityPart.activity_id == "8")
            .values(payload={"heart_rate": 70})
        )
        cases = {
            "retained": retained,
            "decode_error": bad,
            "empty": empty,
            "superseded": superseded,
            "unchanged": clean,
            "bad_archive": bad_archive,
            "forced": forced,
            "zipped": zipped,
        }
        for name, row in cases.items():
            if name != "unchanged":
                row.parser_version = 8
        session.execute(
            text("""
                INSERT INTO module_configs
                    (id,owner_id,pack_key,tracking_enabled,collection_enabled,
                     reminders_enabled,visible,llm_enabled,settings,revision)
                SELECT gen_random_uuid(),id,'training',false,false,false,false,false,'{}',1
                FROM people
            """)
        )
        return {name: str(row.id) for name, row in cases.items()}


def snapshot(engine, refs):
    with Session(engine) as session:
        raw = session.scalars(select(SourcePayload)).all()
        names = {str(row.id): (row.source_key, row.payload_hash) for row in raw}

        def stable(value):
            if isinstance(value, str):
                return names.get(value, value)
            if isinstance(value, dict):
                return {key: stable(item) for key, item in value.items() if key != "at"}
            return value

        return {
            "activities": {
                row.id: [row.fit_key, stable(row.details)]
                for row in session.scalars(select(Activity))
            },
            "parts": [
                (row.activity_id, row.kind, row.sequence, row.payload)
                for row in session.scalars(
                    select(ActivityPart).order_by(
                        ActivityPart.activity_id, ActivityPart.kind, ActivityPart.sequence
                    )
                )
            ],
            "raw": {
                (raw.source_key, raw.payload_hash): [
                    raw.status,
                    raw.parser_version,
                    raw.archive_key,
                ]
                for raw in raw
            },
            "watermarks": {
                row.key: stable(row.value)
                for row in session.scalars(
                    select(AppState).where(AppState.key.startswith("fit-version:"))
                )
            },
            "replay": {
                name: stable(session.get(AppState, f"replay:{identity}:9").value)
                for name, identity in refs.items()
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
            python_archive = LocalArchive(root / "python")
            native_archive = LocalArchive(root / "native")
            py_refs = seed(python, python_archive)
            native_refs = seed(native, native_archive)
            for name, identity in py_refs.items():
                try:
                    replay.run_replay(
                        python,
                        python_archive,
                        SimpleNamespace(timezone=CURRENT_ZONE),
                        {"raw_ref": identity, "account": ACCOUNT, "target_version": 9},
                    )
                except RuntimeError:
                    assert name in {"bad_archive", "decode_error"}, name
            expected = snapshot(python, py_refs)
            assert expected["replay"]["retained"]["status"] == "normalized"
            assert expected["replay"]["zipped"]["rows"] == 2
            assert expected["watermarks"]["fit-version:1"]["latest_attempt"]
            path = root / "fit-replay.json"
            path.write_text(
                json.dumps(
                    [
                        {"name": name, "id": identity, "status": expected["replay"][name]["status"]}
                        for name, identity in native_refs.items()
                    ]
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [binary, "--ignored"],
                check=True,
                env={
                    **os.environ,
                    "GA_TEST_DATABASE_URL": native_url,
                    "GA_TEST_FIT_REPLAY": str(path),
                    "GA_TEST_FIT_REPLAY_ARCHIVE": str(native_archive.root),
                },
            )
            actual = snapshot(native, native_refs)
            assert actual == expected, {
                key: (actual[key], expected[key])
                for key in expected
                if actual[key] != expected[key]
            }
            print(f"Matched {len(native_refs)} FIT replay cases with pinned Python")
    finally:
        python.dispose()
        native.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
