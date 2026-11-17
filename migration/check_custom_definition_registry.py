"""Compare tracker-definition transitions and metric mappings with the pinned baseline."""

import argparse
import copy
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from garmin_ai.definitions import (
    activate_definition,
    create_definition_draft,
    propose_definition_revision,
    retire_definition,
)
from garmin_ai.models import (
    EventDefinition,
    EventDefinitionVersion,
    EventMetricMapping,
    MetricDefinition,
    MetricDefinitionVersion,
    PendingQuestion,
    TrackerConfig,
)

ACTOR = "synthetic-admin"
AUTH = {"actor": ACTOR, "authorized": True}
SPEC = {
    "key": "user.synthetic_tracker",
    "labels": {"ru": "Пример", "zh-Hant": "示例"},
    "topology": "bounded_interval",
    "derived_duration": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "choice": {"type": "string", "enum": ["one", "two"]},
            "confirmed": {"type": "boolean"},
            "rating": {"$ref": "#/$defs/rating", "minimum": 2},
            "details": {"type": "string", "maxLength": 20},
        },
        "$defs": {"rating": {"type": "integer", "minimum": 1, "maximum": 5}},
        "required": ["choice", "rating"],
    },
    "fields": {
        name: {"id": f"user.synthetic_{name}", "semantic": semantic, "labels": {"en": name}}
        for name, semantic in [
            ("choice", "nominal"),
            ("confirmed", "boolean"),
            ("rating", "ordinal"),
            ("details", "text"),
        ]
    },
}
SPEC["fields"]["rating"]["unit"] = "score"
TIMESTAMP = datetime(2020, 4, 15, tzinfo=UTC)


def seed(engine):
    with Session(engine) as session, session.begin():
        draft = create_definition_draft(session, SPEC, **AUTH)
        tracker = TrackerConfig(owner_id=draft.owner_id, definition_id=draft.id)
        session.add(tracker)
        session.flush()
        for state in ["pending", "sending", "uncertain", "sent"]:
            session.add(
                PendingQuestion(
                    kind="tracker_reminder",
                    text="Synthetic question",
                    evidence={"tracker_id": str(tracker.id)},
                    priority=1.0,
                    earliest_send_at=TIMESTAMP,
                    expires_at=TIMESTAMP + timedelta(days=1),
                    status=state,
                    dedup_key=f"synthetic:{state}",
                )
            )
        activate_definition(session, draft.id, draft.revision, **AUTH)
        return draft.id, draft.revision


def change_v2():
    revised = copy.deepcopy(SPEC)
    revised["schema"]["$defs"]["rating"]["maximum"] = 8
    revised["schema"]["properties"]["choice"] = {
        "enum": ["one", "two", "three", None],
    }
    return revised


def write_python(engine, identity, revision):
    changed = change_v2()
    invalid = copy.deepcopy(changed)
    invalid["fields"]["rating"]["unit"] = "broken"
    with Session(engine) as session, session.begin():
        draft = propose_definition_revision(session, identity, revision, invalid, **AUTH)
        revision = draft.revision
    try:
        with Session(engine) as session, session.begin():
            activate_definition(session, identity, revision, **AUTH)
    except (ValueError, KeyError):
        pass
    else:
        raise AssertionError("Unexpected successful invalid activation")
    with Session(engine) as session, session.begin():
        draft = propose_definition_revision(session, identity, revision, changed, **AUTH)
        activate_definition(session, identity, draft.revision, **AUTH)
        retire_definition(session, identity, draft.revision, authorized=True)
        fresh = copy.deepcopy(changed)
        fresh["key"] = "user.synthetic_fresh_definition"
        definition = create_definition_draft(session, fresh, **AUTH)
        activate_definition(session, definition.id, definition.revision, **AUTH)


def snapshot(engine):
    with Session(engine) as session:
        definitions = {
            definition.key: (
                definition.status,
                definition.current_version,
                definition.revision,
                draft_shape,
            )
            for definition, draft_shape in session.execute(
                select(EventDefinition, func.jsonb_typeof(EventDefinition.draft))
            )
        }
        versions = {
            (key, version.version): (
                version.schema_hash,
                version.field_metadata,
                version.schema,
                version.labels,
                version.topology,
                version.privacy,
                version.allowed_operations,
            )
            for key, version in session.execute(
                select(EventDefinition.key, EventDefinitionVersion).join(EventDefinitionVersion)
            )
        }
        metrics = {
            (key, version.version): (
                version.schema_hash,
                version.scale_version,
                version.minimum,
                version.maximum,
                version.category_domain,
                version.labels,
                version.value_kind,
            )
            for key, version in session.execute(
                select(MetricDefinition.key, MetricDefinitionVersion).join(MetricDefinitionVersion)
            )
        }
        mappings = {
            (version, field, projection): (key, number)
            for version, field, projection, key, number in session.execute(
                select(
                    EventDefinitionVersion.version,
                    EventMetricMapping.field_id,
                    EventMetricMapping.projection_version,
                    MetricDefinition.key,
                    MetricDefinitionVersion.version,
                )
                .select_from(EventMetricMapping)
                .join(EventDefinitionVersion)
                .join(MetricDefinitionVersion)
                .join(
                    MetricDefinition, MetricDefinition.id == MetricDefinitionVersion.definition_id
                )
            )
        }
        reminders = dict(
            session.execute(select(PendingQuestion.dedup_key, PendingQuestion.status))
            .tuples()
            .all()
        )
        return definitions, versions, metrics, mappings, reminders


def run(binary):
    source = os.environ["GA_TEST_DATABASE_URL"]
    rust_target = os.environ["GA_TEST_RUST_DATABASE_URL"]
    if rust_target == source:
        raise ValueError("Use two distinct disposable databases")
    python = isolated_engine()
    try:
        write_python(python, *seed(python))
        os.environ["GA_TEST_DATABASE_URL"] = rust_target
        rust = isolated_engine()
        try:
            seed(rust)
            subprocess.run(
                [binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_TRACKER_SPEC": json.dumps(change_v2())},
            )
            with Session(rust) as session:
                draft = session.scalar(
                    select(EventDefinition).where(EventDefinition.key == SPEC["key"])
                )
                assert draft is not None
                assert draft.status == "retired"
            for expected, actual in zip(snapshot(python), snapshot(rust), strict=True):
                assert actual.keys() == expected.keys(), (actual.keys(), expected.keys())
                for key, values in expected.items():
                    assert actual[key] == values, (key, actual[key], values)
            print("Tracker definitions, metric versions, mappings and reminder states match Python")
        finally:
            rust.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = source
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
