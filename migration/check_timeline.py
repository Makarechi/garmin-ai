"""Compare layered native timelines against the pinned Python service."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.events import EventInput, create_event
from garmin_ai.models import Activity, TimelineInterval
from garmin_ai.queries import timeline

START = datetime(2026, 10, 25, 9, tzinfo=UTC)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
SIZE_LIMIT = "Timeline exceeds the size limit; narrow the interval"


def add_interval(session, name, start, end, label, source, confirmed, evidence=None):
    session.add(
        TimelineInterval(
            id=name,
            start=start,
            end=end,
            label=label,
            source=source,
            confirmed=confirmed,
            confidence=0.9,
            evidence=evidence if evidence is not None else {"synthetic": name},
        )
    )


def seed(engine):
    with Session(engine) as session, session.begin():
        for name, kind, begin, finish in [
            ("run", "running", START - 4 * MIN, START + 20 * MIN),
            ("ride", "cycling", START + 8 * MIN, START + 30 * MIN),
            ("edge", "walking", START + HOUR, START + HOUR + MIN),
        ]:
            session.add(Activity(id=name, kind=kind, start=begin, end=finish, timezone="UTC"))
        for name, begin, finish, label, source, confirmed, evidence in [
            ("sleep", START - HOUR, START + 2 * MIN, "sleep", "garmin_connect", True, None),
            ("plan", START, START + 38 * MIN, "meeting", "calendar", True, {"z": 3, "a": 8}),
            (
                "remote",
                START + 30 * MIN,
                START + 42 * MIN,
                "visit",
                "external_calendar",
                False,
                None,
            ),
            ("nap", START + 34 * MIN, START + 45 * MIN, "nap", "file_import", True, None),
            ("legacy", START + 30 * MIN, START + 40 * MIN, "commute", "manual", False, None),
            (
                "ascii",
                START + 30 * MIN,
                START + 40 * MIN,
                "synthetic one",
                "manual",
                True,
                {"c": 1},
            ),
            (
                "unicode",
                START + 30 * MIN,
                START + 40 * MIN,
                "synthetic two",
                "manual",
                True,
                {"🧪": 1},
            ),
            ("unrelated", START + 2 * HOUR, START + 3 * HOUR, "travel", "calendar", True, None),
        ]:
            add_interval(session, name, begin, finish, label, source, confirmed, evidence)
        events = [
            (START - HOUR, None, {"type": "migraine"}, "confirmed"),
            (START + MIN, None, {"type": "note", "description": "synthetic point"}, "confirmed"),
            (
                START + 15 * MIN,
                START + 16 * MIN,
                {"type": "illness", "description": "synthetic symptom"},
                "confirmed",
            ),
            (
                START + 29 * MIN,
                START + 32 * MIN,
                {"type": "context", "description": "some context"},
                "inferred",
            ),
            (START + 31 * MIN, None, {"type": "medication"}, "confirmed"),
            (
                START + 48 * MIN,
                START + 50 * MIN,
                {"type": "nap", "description": "synthetic nap"},
                "confirmed",
            ),
            (START + HOUR, None, {"type": "note", "description": "excluded boundary"}, "confirmed"),
        ]
        for begin, finish, payload, status in events:
            create_event(
                session,
                EventInput(start=begin, end=finish, payload=payload, status=status),
                actor="synthetic",
            )
        create_event(
            session,
            EventInput(start=START + 4 * MIN, payload={"type": "note", "description": "deleted"}),
            actor="synthetic",
        ).deleted = True
        for i in range(499):
            at = START + timedelta(days=2)
            add_interval(session, f"max-{i}", at, at + MIN, "meeting", "calendar", False, {})
        add_interval(
            session,
            "extra",
            START + timedelta(days=2, minutes=1),
            START + timedelta(days=2, minutes=2),
            "meeting",
            "calendar",
            False,
        )
        for i in range(150):
            at = START + timedelta(days=4, seconds=i)
            add_interval(
                session,
                f"dense-{i}",
                at,
                at + timedelta(minutes=5, seconds=i),
                "synthetic overlap",
                "calendar",
                True,
                {"body": "x" * 2500},
            )
        session.execute(
            text("""
                INSERT INTO timeline_intervals(id, start, "end", label, source, confidence, confirmed, evidence)
                VALUES ('large-evidence', :start, :finish, 'synthetic', 'calendar', 0.9, true,
                        jsonb_build_object('body', repeat('x', 4194305)))
            """),
            {"start": START + timedelta(days=5), "finish": START + timedelta(days=5, minutes=1)},
        )


def fixtures(engine):
    seed(engine)
    offset = timezone(2 * HOUR)
    offset_west = timezone(-4 * HOUR)
    cases = [
        ("empty", START - timedelta(days=1), START - timedelta(hours=23), None),
        ("all_layers", START, START + HOUR, None),
        ("window_inside_open_episode", START + 47 * MIN, START + 48 * MIN, None),
        ("point_at_left", START + MIN, START + 2 * MIN, None),
        ("offset_bounds", START.astimezone(offset), (START + HOUR).astimezone(offset_west), None),
        ("same_instants", START, START, None),
        ("reversed_cross_date", START, START - timedelta(days=1), None),
        ("within_one_day_of_limit", START, START + timedelta(days=32, microseconds=1), None),
        ("beyond_calendar_limit", START, START + timedelta(days=33), None),
        ("absolute_calendar_edge", datetime(1, 2, 28, tzinfo=UTC), START, None),
        ("exactly_500", START + timedelta(days=2), START + timedelta(days=2, minutes=1), None),
        ("combined_limit", START + timedelta(days=2), START + timedelta(days=2, minutes=2), None),
        (
            "dense_size_limit",
            START + timedelta(days=4),
            START + timedelta(days=4, hours=1),
            SIZE_LIMIT,
        ),
        (
            "evidence_size_limit",
            START + timedelta(days=5),
            START + timedelta(days=5, hours=1),
            SIZE_LIMIT,
        ),
    ]
    results = []
    with Session(engine) as session:
        for name, start, end, error in cases:
            result = None
            if error is None:
                try:
                    result = timeline(session, start, end)
                except ValueError as exc:
                    error = str(exc)
            results.append(
                {
                    "name": name,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "error": error,
                    "expected": result,
                }
            )
    annotated = results[1]["expected"]
    assert annotated["layers"]["plans"][0]["status"] == "planned"
    assert annotated["layers"]["wellbeing"][0]["end"] is None
    assert not any(
        annotation["annotation_id"] in segment["annotations"]
        for annotation in annotated["layers"]["context"]
        if annotation.get("topology") == "point"
        for segment in annotated["segments"]
    )
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        tests = fixtures(engine)
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "timeline.json"
            destination.write_text(json.dumps(tests), encoding="utf-8")
            subprocess.run(
                [args.binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_TIMELINE": str(destination)},
            )
        print(f"Passed {len(tests) - 2} paired timelines and 2 native size-limit cases")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
