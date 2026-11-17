"""Compare owner diary pagination with pinned Python on synthetic event facts."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.definitions import ensure_system_definition
from garmin_ai.models import Event, EventDefinition, EventDefinitionVersion, Person
from garmin_ai.queries import list_events

START = datetime(2026, 10, 25, 9, tzinfo=UTC)
END = START + timedelta(hours=6)
HOUR = timedelta(hours=1)


def seed(engine):
    with Session(engine) as session, session.begin():
        owner = session.scalar(select(Person.id))
        ensure_system_definition(session, "caffeine")

        def custom(key, status, operations):
            definition = EventDefinition(owner_id=owner, namespace="user", key=key, status=status)
            session.add(definition)
            session.flush()
            versions = [
                EventDefinitionVersion(
                    definition_id=definition.id,
                    version=index,
                    schema={},
                    schema_hash="0" * 64,
                    topology="point",
                    field_metadata={},
                    labels={"en": "Synthetic diary kind"},
                    privacy="sensitive",
                    allowed_operations=allowed,
                )
                for index, allowed in enumerate(operations, 1)
            ]
            session.add_all(versions)
            session.flush()
            return [version.id for version in versions]

        first, second = custom("user.synthetic", "retired", [["query", "update"], ["query"]])
        (hidden,) = custom("user.hidden", "active", [["create"]])
        custom("user.draft", "draft", [])

        def event(number, kind, at, end=None, topology="point", **other):
            session.add(
                Event(
                    id=UUID(int=number),
                    kind=kind,
                    start=at,
                    end=end,
                    topology=topology,
                    timezone="Asia/Tokyo",
                    source="manual",
                    original_text="Synthetic quote ☕",
                    time_precision="interval" if topology.endswith("interval") else "instant",
                    created_at=START,
                    recorded_at=START + timedelta(microseconds=13),
                    ingested_at=START + timedelta(microseconds=150),
                    updated_at=START + timedelta(microseconds=300),
                    **other,
                )
            )

        event(1, "migraine", START - 2 * HOUR, topology="open_interval", payload={})
        event(2, "illness", START - HOUR, START + HOUR, "bounded_interval", payload={})
        event(3, "note", START - 2 * HOUR, payload={})
        event(4, "note", START - HOUR, START, "bounded_interval", payload={})
        event(5, "note", START, START, payload={}, status="needs_confirmation")
        event(6, "note", END, payload={})
        event(7, "note", START + HOUR, payload={}, status="inferred")
        event(8, "note", START, payload={}, deleted=True)
        event(9, "user.hidden", START, definition_version_id=hidden, payload={"invisible": True})
        event(10, "user.synthetic", START, definition_version_id=first, payload={"count": 2})
        event(11, "user.synthetic", START, definition_version_id=second, payload={"count": 3})
        event(12, "user.synthetic", START, payload={"count": 4})
        for number, basis, servings in [
            (13, "per_serving", 2.5),
            (14, "total", 3),
            (15, "unknown", 4),
        ]:
            event(
                number,
                "caffeine",
                START + HOUR,
                payload={
                    "dose_basis": basis,
                    "dose_provenance": "reported_label",
                    "servings": servings,
                    "caffeine_mg_min": 10,
                    "caffeine_mg_estimate": 40,
                },
            )
        event(16, "caffeine", START + 2 * HOUR, payload={"caffeine_mg_estimate": 55})
        event(17, "context", START + HOUR, topology="flexible", payload={})


def run(binary):
    engine = isolated_engine()
    try:
        seed(engine)
        cases = [
            (START, END, None, 1000),
            (START, END, None, 1),
            (START, END, None, 3),
            (START, END, "note", 1000),
            (START, END, "user.synthetic", 1000),
            (START, END, "user.synthetic", 1),
            (START, END, "user.hidden", 1000),
            (START, END, "user.draft", 1000),
            (START, END, "caffeine", 1000),
            (START, END, "system.caffeine", 1000),
            (START, END, "sleep_override", 1000),
            (START, END, "unknown", 1000),
            (START, END, "", 1000),
            (START, END, None, 0),
            (START, END, None, 1001),
            (START, END, "unknown", 0),
            (START, START, None, 1),
            (START, START - HOUR, None, 1),
            (START, START - timedelta(days=1), None, 1),
            (START, START + timedelta(days=3660), None, 1),
            (START, START + timedelta(days=3660, microseconds=1), None, 1),
            (START, START + timedelta(days=3662), None, 1),
            (datetime(1, 3, 1, tzinfo=UTC), END, None, 1),
        ]
        requests = []
        expected = []
        with Session(engine) as session:
            for start, end, kind, limit in cases:
                requests.append(
                    {
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "kind": kind,
                        "limit": limit,
                    }
                )
                try:
                    expected.append(list_events(session, start, end, kind, limit))
                except ValueError as exc:
                    expected.append({"error": str(exc)})

        assert expected[1]["rows"][0]["id"] == str(UUID(int=5))
        assert expected[1]["truncated"]
        assert expected[4]["rows"][0]["can_update"]
        assert not expected[4]["rows"][1]["can_update"]
        assert expected[6]["rows"] == []
        assert expected[8] == expected[9]
        totals = [row["caffeine_total"]["estimate"] for row in expected[8]["rows"]]
        assert totals == [100, 40, None, None]
        assert expected[0]["rows"][-2]["missing_end"]
        assert all(row["kind"] != "user.hidden" for row in expected[0]["rows"])

        with TemporaryDirectory() as directory:
            path = Path(directory) / "diary-reads.json"
            path.write_text(json.dumps(requests), encoding="utf-8")
            output = subprocess.run(
                [binary, "--ignored", "--nocapture"],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "GA_TEST_DIARY_READS": str(path)},
            ).stdout
        prefix = "diary: "
        actual = [
            json.loads(line.removeprefix(prefix))
            for line in output.splitlines()
            if line.startswith(prefix)
        ]
        assert len(actual) == len(expected)
        for index, (a, b) in enumerate(zip(actual, expected, strict=True)):
            assert a == b, (index, a, b)
        print(f"Matched {len(actual)} owner diary pages and errors")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
