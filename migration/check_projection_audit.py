"""Compare the two read-only projection audit commands on the same synthetic history."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy import select, text

from garmin_ai.db import transaction
from garmin_ai.definitions import (
    CustomEntryInput,
    DefinitionSpec,
    activate_definition,
    create_custom_event,
    create_definition_draft,
    update_custom_event,
)
from garmin_ai.events import EventInput, create_event, delete_event
from garmin_ai.metric_definitions import (
    CoveragePolicy,
    MetricSpec,
    bind_event_field,
    register_metric_definition,
)
from garmin_ai.models import Audit, MetricObservation

KEY = "user.synthetic_audit"
FIELD = f"{KEY}.level"
UNIT = "score_1-5"
AT = datetime(2026, 10, 25, 0, 45, tzinfo=UTC)


def fixture(engine):
    with transaction(engine) as session:
        definition = create_definition_draft(
            session,
            DefinitionSpec(
                key=KEY,
                labels={"en": "Synthetic audit"},
                topology="bounded_interval",
                schema={
                    "type": "object",
                    "properties": {"level": {"type": "integer", "minimum": 1, "maximum": 5}},
                    "required": ["level"],
                    "additionalProperties": False,
                },
                fields={
                    "level": {
                        "id": FIELD,
                        "labels": {"en": "Level"},
                        "semantic": "ordinal",
                        "unit": UNIT,
                    }
                },
            ),
            actor="fixture",
            authorized=True,
        )
        version = activate_definition(
            session, definition.id, definition.revision, actor="fixture", authorized=True
        )
        metric = register_metric_definition(
            session,
            MetricSpec(
                key=FIELD,
                labels={"en": "Synthetic level"},
                value_kind="ordinal",
                dimension="ordinal",
                unit=UNIT,
                scale_id="user.synthetic-audit",
                scale_version=1,
                aggregation="distribution",
                allowed_methods={"distribution", "median", "latest"},
                coverage=CoveragePolicy(kind="sparse"),
                time_semantics="interval",
                minimum=1,
                maximum=5,
            ),
            authorized=True,
        )
        bind_event_field(session, version.id, FIELD, metric.id, authorized=True)

        def entry(index, value=4, status="confirmed"):
            start = AT + timedelta(minutes=20 * index)
            return CustomEntryInput(
                definition_key=KEY,
                start=start,
                end=start + timedelta(minutes=25),
                timezone="Europe/Bratislava",
                values={"level": value},
                units={"level": UNIT},
                status=status,
            )

        events = [
            create_custom_event(
                session,
                entry(index, status="needs_confirmation" if index == 4 else "confirmed"),
                actor="fixture",
            )
            for index in range(7)
        ]
        updated = update_custom_event(
            session, events[6].id, entry(6, 5), revision=events[6].revision, actor="fixture"
        )
        delete_event(session, events[5].id, revision=events[5].revision, actor="fixture")
        system = create_event(
            session,
            EventInput(start=AT, timezone="UTC", payload={"type": "note", "description": "sample"}),
            actor="fixture",
        )

        def observation(event, *, valid=True):
            return session.scalars(
                select(MetricObservation)
                .where(
                    MetricObservation.source_entry_id == event.id,
                    MetricObservation.valid.is_(valid),
                )
                .order_by(MetricObservation.projection_version.desc())
            ).one()

        observation(events[1]).valid = False
        intermediate = observation(events[2])
        intermediate.sequence = 7
        intermediate.quality = "estimated"
        observation(events[3]).projection_version = None
        observation(events[5], valid=False).valid = True
        active = observation(updated)
        assert active.projection_version == 2
        audit = session.scalars(select(Audit).where(Audit.event_id == updated.id)).all()
        assert len(audit) == 2
        audit[1].created_at = audit[0].created_at
        return [str(event.id) for event in events], str(system.id)


def snapshot(engine):
    with engine.connect() as connection:
        return [
            connection.execute(
                text(
                    "SELECT md5(string_agg(to_jsonb(item)::text, '' ORDER BY id)) FROM "
                    + table
                    + " item"
                )
            ).scalar_one()
            for table in ("events", "metric_observations", "audit_log")
        ]


def run(prefix, *args):
    return subprocess.run(
        [*prefix, "projection-audit", *args],
        env={"GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"]},
        capture_output=True,
        timeout=15,
        check=False,
        text=True,
    )


def verify(engine, binary):
    ids, system = fixture(engine)
    before = snapshot(engine)
    python = [sys.executable, "-m", "garmin_ai.cli"]
    native = [str(binary)]
    for flags, size in (((), 500), (("--limit", "3"), 3), (("--limit", "1"), 1)):
        expected, actual = (run(command, *flags) for command in (python, native))
        assert expected.returncode == actual.returncode == 0, (expected.stderr, actual.stderr)
        assert not expected.stderr and not actual.stderr
        pages = [json.loads(line) for line in actual.stdout.splitlines()]
        assert pages == [json.loads(line) for line in expected.stdout.splitlines()]
        rows = {row["event_id"]: row for page in pages[:-1] for row in page["rows"]}
        assert system not in rows and set(rows) == set(ids)
        assert all(len(page["rows"]) <= size and not page["writes"] for page in pages[:-1])
        assert rows[ids[1]]["missing"] == 1
        assert rows[ids[2]]["mismatched"] == rows[ids[3]]["mismatched"] == 1
        assert rows[ids[4]]["pending"] and rows[ids[5]]["stale"] == 1
        assert rows[ids[6]]["history_unknown"] and not rows[ids[6]]["mismatched"]

    for limit in ("0", "-1", "1001"):
        for command in (python, native):
            result = run(command, "--limit", limit)
            assert result.returncode == 1 and not result.stdout
    assert before == snapshot(engine)
    print(
        "Matched read-only projection audit pages, drift, history status, limits and unchanged facts"
    )


def verify_snapshot(engine, binary):
    # Backpressure keeps the last pages unread until after a separate writer commits.
    with transaction(engine) as session:
        for offset in range(96):
            start = AT + timedelta(days=1, minutes=offset)
            last = create_custom_event(
                session,
                CustomEntryInput(
                    definition_key=KEY,
                    start=start,
                    end=start + timedelta(minutes=25),
                    timezone="UTC",
                    values={"level": 4},
                    units={"level": UNIT},
                    status="confirmed",
                ),
                actor="fixture",
            ).id
    before_write = run([sys.executable, "-m", "garmin_ai.cli"], "--limit", "1")
    assert before_write.returncode == 0 and len(before_write.stdout) > 8192
    with subprocess.Popen(
        [binary, "projection-audit", "--limit", "1"],
        env={"GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"]},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            fcntl.fcntl(process.stdout, fcntl.F_SETPIPE_SZ, 4096)
            first = process.stdout.readline()
            assert json.loads(first)["page"] == 1
            with engine.begin() as writer:
                writer.execute(
                    text("UPDATE metric_observations SET value=5 WHERE source_entry_id=:event"),
                    {"event": last},
                )
            remaining, error = process.communicate(timeout=25)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert process.returncode == 0 and not error
    assert [json.loads(line) for line in (first + remaining).splitlines()] == [
        json.loads(line) for line in before_write.stdout.splitlines()
    ]
    after_write = run([str(binary)], "--limit", "1")
    assert after_write.returncode == 0
    assert json.loads(after_write.stdout.splitlines()[-1])["totals"]["mismatched"] == 3
    print("Verified one audit snapshot across 103 pages and a concurrent committed change")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        binary = args.binary.resolve(strict=True)
        verify(engine, binary)
        verify_snapshot(engine, binary)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
