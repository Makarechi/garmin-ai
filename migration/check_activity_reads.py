"""Compare native activity pages with the pinned application on synthetic rows."""

import argparse
import json
import math
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.fit_messages import NON_SAMPLE_FIT_KINDS
from garmin_ai.models import Activity, ActivityPart
from garmin_ai.queries import activity_details, list_activities

START = datetime(2026, 10, 25, 9, tzinfo=UTC)


def seed(engine):
    with Session(engine) as session, session.begin():
        for identity, kind, offset in [
            ("previous", "running", -1),
            ("run", "running", 0),
            ("trainer", "indoor_cycling", 1),
            ("bulky", "cycling", 2),
            ("edge", "trail_running", 3),
        ]:
            at = START + timedelta(hours=offset)
            session.add(
                Activity(
                    id=identity,
                    name=None if identity == "trainer" else f"Synthetic {identity}",
                    kind=kind,
                    start=at,
                    end=at + timedelta(minutes=45),
                    timezone="Europe/Bratislava",
                    details={"synthetic": [True, "テスト"]},
                    duration_seconds=2700,
                    distance_m=4251.1,
                    avg_hr=0.0,
                    ascent_m=-0.0,
                    updated_at=START + timedelta(microseconds=325),
                )
            )
        session.flush()
        for kind in sorted(
            NON_SAMPLE_FIT_KINDS
            | {
                "fit_record",
                "fit_vendor_unknown",
                "fitXlegacy",
                "activity_details",
                "activity_weather",
                "activity_splits",
            }
        ):
            session.add(
                ActivityPart(
                    activity_id="run",
                    kind=kind,
                    sequence=0,
                    payload={"family": kind, "unicode": "🙂"},
                )
            )
        session.add_all(
            [
                ActivityPart(activity_id="run", kind="fit_session", sequence=3, payload=[1, None]),
                ActivityPart(activity_id="run", kind="fit_session", sequence=6, payload={}),
                ActivityPart(
                    activity_id="bulky", kind="activity_weather", sequence=0, payload={"wind": 1}
                ),
            ]
        )
        session.flush()
        session.execute(
            text("""
                INSERT INTO activity_parts(activity_id,kind,sequence,payload)
                VALUES ('bulky','fit_vendor_blob',0,jsonb_build_object('blob',repeat('x',16777216)))
            """)
        )


def run(binary):
    engine = isolated_engine()
    try:
        seed(engine)
        cases = []
        expected = []
        with Session(engine) as session:

            def compare(operation, method, **arguments):
                cases.append(
                    {
                        "operation": operation,
                        **{
                            k: v.isoformat() if isinstance(v, datetime) else v
                            for k, v in arguments.items()
                        },
                    }
                )
                try:
                    expected.append(method(session, **arguments))
                except (LookupError, ValueError) as exc:
                    expected.append({"error": str(exc)})

            for start, end, kind, limit in [
                (START, START + timedelta(hours=3), None, 2),
                (START, START + timedelta(hours=3), "", 1000),
                (START, START + timedelta(hours=3), "running", 1),
                (START, START + timedelta(hours=3), "missing", 1),
                (START, START + timedelta(days=3660), None, 1000),
                (START, START + timedelta(days=3660, microseconds=1), None, 1),
                (START, START, None, 100),
                (START, START - timedelta(minutes=1), None, 1),
                (START, START + timedelta(hours=1), None, 0),
                (datetime(1, 3, 1, tzinfo=UTC), datetime(1, 3, 2, tzinfo=UTC), None, 1),
            ]:
                compare("list", list_activities, start=start, end=end, kind=kind, limit=limit)

            for identity, include_samples, offset, limit in [
                ("run", False, 0, 2000),
                ("run", True, 0, 2000),
                ("run", False, 0, 1),
                ("run", False, 1, 2),
                ("run", False, len(NON_SAMPLE_FIT_KINDS) + 3, 4),
                ("run", True, 1_000_000, 100),
                ("run", True, 1_000_001, 100),
                ("run", True, 0, 2001),
                ("run", True, 0, 0),
                ("trainer", False, 0, 100),
                ("missing", True, 0, 2000),
                ("bulky", False, 0, 10),
                ("bulky", True, 0, 1),
            ]:
                compare(
                    "details",
                    activity_details,
                    activity_id=identity,
                    include_samples=include_samples,
                    offset=offset,
                    limit=limit,
                )

        assert [row["id"] for row in expected[0]["rows"]] == ["run", "trainer"]
        assert expected[0]["truncated"]
        assert {part["kind"] for part in expected[10]["parts"]} == NON_SAMPLE_FIT_KINDS | {
            "activity_weather",
            "activity_splits",
        }
        assert expected[-1]["next_offset"] == 1
        with TemporaryDirectory() as directory:
            path = Path(directory) / "activity-reads.json"
            path.write_text(json.dumps(cases), encoding="utf-8")
            output = subprocess.run(
                [binary, "--ignored", "--nocapture"],
                check=True,
                text=True,
                capture_output=True,
                env={**os.environ, "GA_TEST_ACTIVITY_READS": str(path)},
            ).stdout
        prefix = "activity: "
        actual = [
            json.loads(line.removeprefix(prefix))
            for line in output.splitlines()
            if line.startswith(prefix)
        ]
        assert len(actual) == len(expected)
        for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
            assert left == right, (index, left, right)
        assert math.copysign(1, actual[0]["rows"][0]["ascent_m"]) == -1
        print(f"Matched {len(actual)} activity pages and errors")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
