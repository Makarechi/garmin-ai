"""Compare native activity summary recovery against pinned Python."""

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
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from garmin_ai import replay
from garmin_ai.archive import LocalArchive
from garmin_ai.events import serialize
from garmin_ai.ingest import ingest
from garmin_ai.models import Activity, AppState, Insight, SourcePayload


def summary(identity, second=0, **fields):
    return {
        "activityId": identity,
        "startTimeGMT": (AT + timedelta(seconds=second)).replace(tzinfo=None).isoformat(" "),
        "duration": 120,
        "timeZoneUnitDTO": {"timeZone": ORIGINAL_ZONE},
        **fields,
    }


def seed(engine, archive):
    with Session(engine) as session, session.begin():
        session.add(AppState(key="account:garmin", value={"fingerprint": ACCOUNT}))

        def save(endpoint, name, payload, sequence):
            result = ingest(
                session,
                archive,
                endpoint,
                name,
                payload,
                ORIGINAL_ZONE,
                fetched_at=AT + timedelta(minutes=sequence),
            )
            return session.get(SourcePayload, UUID(result["source_ref"]))

        page = save(
            "activities",
            "page-retained",
            [
                summary(2, calories=34),
                summary(1, activityName="original name", calories=27, distance=600),
            ],
            1,
        )
        save(
            "activity",
            "1",
            summary(
                1,
                480,
                distance=420,
                activityType={"typeKey": "cycling"},
                timeZoneUnitDTO={"timeZone": "UTC"},
            ),
            2,
        )
        first = session.get(Activity, "1")
        first.calories = 999
        first.name = "drifted name"
        first.ascent_m = 10
        session.get(AppState, "activity-version:1").value = {
            **session.get(AppState, "activity-version:1").value,
            "owners": {
                **session.get(AppState, "activity-version:1").value["owners"],
                "ascent_m": str(page.id),
            },
        }
        # The SQL mirror has diverged. This remains the registered canonical archive.
        page.payload = [
            summary(1, calories=404, activityName="incorrect SQL body"),
            summary(2, calories=405),
        ]

        unchanged = save("activity", "3", summary(3, averageHR=81), 3)
        session.get(Activity, "3").avg_hr = 0
        first_page = save(
            "activities",
            "page-empty",
            [summary(4, activityName="retained name")],
            4,
        )
        empty = save("activities", "page-empty", [], 5)
        session.get(Activity, "4").name = "drifted before empty"

        malformed = save(
            "activities",
            "page-invalid",
            [summary(5), summary(6, duration=-1)],
            6,
        )
        assert malformed.status == "error"
        old = save("activities", "legacy-older", [summary(7, distance=450)], 7)
        legacy = save("activities", "legacy-newer", [summary(7, distance=700)], 8)
        session.get(Activity, "7").distance_m = 450
        stamp = session.get(AppState, "activity-version:7")
        stamp.value = {"requested_at": stamp.value["requested_at"]}
        assert old.id != legacy.id

        unzoned = summary(9, averageHR=90)
        del unzoned["timeZoneUnitDTO"]
        missing_timezone = save("activities", "page-unzoned", [unzoned], 9)
        session.execute(delete(Activity).where(Activity.id == "9"))
        session.execute(
            delete(AppState).where(AppState.key == f"ingest-meta:{missing_timezone.id}")
        )

        forced = save(
            "activities",
            "page-transient",
            [summary(10, calories=51), summary(11, calories=52)],
            10,
        )
        session.execute(update(Activity).where(Activity.id.in_(["10", "11"])).values(calories=0))
        cases = {
            "unchanged_single": unchanged,
            "unchanged_repeat": unchanged,
            "retained_page": page,
            "retained_after_empty": first_page,
            "empty_attempt": empty,
            "invalid_page": malformed,
            "legacy_field_owner": legacy,
            "missing_timezone": missing_timezone,
            "forced": forced,
        }
        for name, row in cases.items():
            if not name.startswith("unchanged"):
                row.parser_version = 8
        session.add(
            Insight(
                category="activity",
                statement="synthetic activity insight",
                evidence={},
                sample_size=1,
                status="candidate",
                dedup_key="synthetic-activity-insight",
            )
        )
        return {name: str(row.id) for name, row in cases.items()}


def snapshot(engine, refs):
    with Session(engine) as session:
        raw = session.scalars(select(SourcePayload)).all()
        aliases = {str(row.id): row.payload_hash for row in raw}

        def stable(value):
            if isinstance(value, str):
                return aliases.get(value, value)
            if isinstance(value, dict):
                return {key: stable(item) for key, item in value.items()}
            return value

        activities = {}
        for row in session.scalars(select(Activity).order_by(Activity.id)):
            values = serialize(row)
            del values["updated_at"]
            activities[row.id] = values
        state = session.scalars(select(AppState).order_by(AppState.key)).all()
        return {
            "activities": activities,
            "owners": {
                item.key: stable(item.value)
                for item in state
                if item.key.startswith("activity-version:")
            },
            "raw": {row.payload_hash: [row.status, row.parser_version, row.payload] for row in raw},
            "replay": {
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
            python_archive = LocalArchive(root / "python")
            native_archive = LocalArchive(root / "native")
            py_refs = seed(python, python_archive)
            native_refs = seed(native, native_archive)
            for name, identity in py_refs.items():
                if name == "unchanged_repeat":
                    with Session(python) as session, session.begin():
                        session.execute(update(Insight).values(status="candidate"))
                try:
                    replay.run_replay(
                        python,
                        python_archive,
                        SimpleNamespace(timezone=CURRENT_ZONE),
                        {"raw_ref": identity, "account": ACCOUNT, "target_version": 9},
                    )
                except RuntimeError:
                    assert name in {"missing_timezone", "invalid_page"}, name
                if name.startswith("unchanged"):
                    with Session(python) as session:
                        expected_insight = (
                            "superseded" if name == "unchanged_single" else "candidate"
                        )
                        assert session.scalar(select(Insight.status)) == expected_insight
            expected = snapshot(python, py_refs)
            assert expected["activities"]["1"]["ascent_m"] is None
            assert expected["activities"]["1"]["distance_m"] == 420
            assert expected["activities"]["1"]["timezone"] == "UTC"
            assert expected["activities"]["1"]["name"] == "original name"
            assert expected["activities"]["7"]["distance_m"] == 450
            assert expected["owners"]["activity-version:7"]["legacy_owner_fields"] == ["distance_m"]
            path = root / "summary-replay.json"
            path.write_text(
                json.dumps(
                    [
                        {"name": name, "id": identity, "status": expected["replay"][name]}
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
                    "GA_TEST_SUMMARY_REPLAY": str(path),
                    "GA_TEST_SUMMARY_REPLAY_ARCHIVE": str(native_archive.root),
                },
            )
            actual = snapshot(native, native_refs)
            assert actual == expected, {
                key: (actual[key], expected[key])
                for key in expected
                if actual[key] != expected[key]
            }
            print(f"Matched {len(native_refs)} activity summary replay cases with pinned Python")
    finally:
        python.dispose()
        native.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
