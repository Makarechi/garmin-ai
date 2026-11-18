"""Compare the public migrate command with pinned Python registry initialization."""

import argparse
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from check_native_schema import BEFORE_REVISIONS, CURRENT, engine_for, upgrade
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from garmin_ai.models import Event

REGISTRY = (
    """
    SELECT d.key,d.status,d.current_version,d.revision,v.version,v.schema_hash,
           v.schema,v.topology,v.field_metadata,v.labels,v.privacy,v.allowed_operations
    FROM event_definitions d JOIN event_definition_versions v ON v.definition_id=d.id
    ORDER BY d.key,v.version
    """,
    """
    SELECT d.key,d.status,d.current_version,v.version,v.schema_hash,
           v.value_kind,v.unit,v.dimension,v.aggregation,v.allowed_methods,v.coverage_policy,
           v.time_semantics,v.category_domain
    FROM metric_definitions d JOIN metric_definition_versions v ON v.definition_id=d.id
    ORDER BY d.key,v.version
    """,
    """
    SELECT pack_key,tracking_enabled,collection_enabled,reminders_enabled,visible,llm_enabled,
           outcome_goal,settings,revision
    FROM module_configs ORDER BY pack_key
    """,
    """
    SELECT key,value FROM app_state WHERE key LIKE 'registry:%' ORDER BY key
    """,
    """
    SELECT event.kind,event.payload,catalog.key,version.version,version.schema_hash
    FROM events event LEFT JOIN event_definition_versions version ON version.id=event.definition_version_id
    LEFT JOIN event_definitions catalog ON catalog.id=version.definition_id ORDER BY event.id
    """,
    """
    SELECT m.metric,d.key,m.value,m.ingested_at
    FROM measurements m LEFT JOIN metric_definition_versions v ON v.id=m.metric_definition_version_id
    LEFT JOIN metric_definitions d ON d.id=v.definition_id ORDER BY m.ts,m.metric
    """,
)


def command(binary, engine, root, *args, succeeds=True):
    result = subprocess.run(
        (binary, "migrate", *args),
        env={
            "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "GA_LOCK_DIR": str(root / "locks"),
        },
        cwd=root,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if succeeds:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1 and not result.stdout
    return result.stdout.strip()


def bootstrap_snapshot(engine):
    with engine.connect() as connection:
        return tuple(connection.execute(text(statement)).all() for statement in REGISTRY)


def seed(engine, *, invalid=False):
    upgrade(engine, BEFORE_REVISIONS)
    at = datetime(2026, 11, 1, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            Event(
                id=UUID(int=200),
                kind="symptom_observation",
                start=at,
                timezone="UTC",
                source="manual",
                payload={
                    "type": "symptom_observation",
                    "episode_id": str(UUID(int=198)),
                    "impact": "   ",
                }
                if not invalid
                else {"type": "symptom_observation", "impact": ""},
            )
        )
        session.execute(
            text("""
            INSERT INTO measurements(ts,metric,source,local_date,value,unit,quality,details,ingested_at)
            VALUES (:at,'heart_rate_bpm','synthetic',CAST(:at AS date),64,'bpm','observed','{}',:at)
            """),
            {"at": at},
        )


def compare(binary, reference, native, root, *, historical):
    if historical:
        seed(reference)
        seed(native)
    expected_revision = BEFORE_REVISIONS if historical else None
    assert json.loads(command(binary, native, root, "--status")) == {
        "current": False,
        "revision": expected_revision,
    }
    assert not (root / "locks").exists()
    legacy = Path(sys.executable).with_name("garmin-ai")
    assert command(legacy, reference, root) == "Database schema upgraded."
    assert command(binary, native, root) == "Database schema upgraded."
    assert json.loads(command(binary, native, root, "--status")) == {
        "current": True,
        "revision": CURRENT,
    }
    expected = bootstrap_snapshot(reference)
    actual = bootstrap_snapshot(native)
    for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
        assert left == right, (index, len(left), len(right))
    assert tuple(map(len, actual[:3])) == (20 if historical else 19, 11, 6)
    if historical:
        event = actual[4][0]
        assert event[2] == "system.symptom_observation" and event[3] != 1
        assert actual[5][0][1] == "system.heart_rate_bpm"
    command(binary, native, root)
    assert bootstrap_snapshot(native) == actual
    print(f"Matched the pinned CLI from {'historical data' if historical else 'an empty database'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("name_prefix")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    opening = make_url(os.environ["GA_TEST_DATABASE_URL"])
    assert opening.database and opening.database.endswith("_test")
    with ExitStack() as stack:
        for label in ("fresh", "historical"):
            reference, native = [
                engine_for(stack, opening.set(database=f"{args.name_prefix}_{label}_{kind}_test"))
                for kind in ("py", "rs")
            ]
            with TemporaryDirectory() as root:
                compare(
                    binary,
                    reference,
                    native,
                    Path(root).resolve(),
                    historical=label == "historical",
                )
        erased = engine_for(stack, opening.set(database=f"{args.name_prefix}_erased_test"))
        upgrade(erased, BEFORE_REVISIONS)
        with erased.begin() as connection:
            connection.execute(
                text("INSERT INTO app_state(key,value) VALUES ('maintenance:erased','{}')")
            )
        with TemporaryDirectory() as root:
            command(binary, erased, Path(root).resolve())
        with erased.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT
            assert connection.scalar(text("SELECT key FROM app_state")) == "maintenance:erased"
            assert connection.scalar(text("SELECT count(*) FROM event_definitions")) == 0
        invalid = engine_for(stack, opening.set(database=f"{args.name_prefix}_invalid_test"))
        seed(invalid, invalid=True)
        with TemporaryDirectory() as root:
            command(binary, invalid, Path(root).resolve(), succeeds=False)
        with invalid.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == BEFORE_REVISIONS
            )
            assert connection.scalar(text("SELECT to_regclass('measurement_revisions') IS NULL"))
            assert connection.scalar(text("SELECT count(*) FROM event_definitions")) == 0
    print("Erased storage stayed fenced and invalid history rolled back")


if __name__ == "__main__":
    main()
