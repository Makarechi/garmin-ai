"""Compare custom entries and original-version effects with pinned Python."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.accounts import owner
from garmin_ai.definitions import (
    activate_definition,
    create_custom_event,
    create_definition_draft,
    propose_definition_revision,
    update_custom_event,
)
from garmin_ai.metric_definitions import bind_event_field, register_metric_definition
from garmin_ai.models import Audit, Event, EventDefinitionVersion, MetricObservation

KEY = "user.synthetic_custom_entry"
ACTOR = "synthetic-entry"
MARKER = 2**169 + 3
EVIDENCE = [{"kind": "synthetic", "id": "one"}]
START = "2025-10-26T01:30:00+02:00"


def specification(bound, query):
    fields = {
        name: {"id": f"{KEY}.{name}", "semantic": "count", "unit": "count", "labels": {"en": name}}
        for name in ("value", "marker")
    }
    return {
        "key": KEY,
        "topology": "point",
        "labels": {"en": "Synthetic entry"},
        "fields": fields,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["marker", "value"],
            "properties": {
                "marker": {"type": "integer", "minimum": 2**169, "maximum": 2**170},
                "value": {"type": "integer", "minimum": 1, "maximum": bound},
            },
        },
        "allowed_operations": ["create", "delete", "update"] + (["query"] if query else []),
    }


def entry(value, marker=MARKER, unit="count", **extra):
    return {
        "definition_key": KEY,
        "start": START,
        "values": {"value": value, "marker": marker},
        "units": {"value": unit},
        "source": "file_import",
        **extra,
    }


INPUTS = {
    "old_create": entry(2, timezone="Europe/Bratislava"),
    "old_edit": entry(9, source="telegram_text", status="needs_confirmation"),
    "create": entry(3, end=START),
    "adjacent_marker": entry(3, marker=MARKER + 1, end=START),
    "wrong_unit": entry(3, unit="steps"),
}


def configure_metric(session, definition, bound):
    metric = register_metric_definition(
        session,
        {
            "key": f"{KEY}.value",
            "labels": {"en": "Synthetic value"},
            "value_kind": "physical_number",
            "unit": "count",
            "dimension": "count",
            "aggregation": "latest",
            "allowed_methods": ["latest"],
            "coverage": {"kind": "all_values"},
            "time_semantics": "point",
            "minimum": 1,
            "maximum": bound,
        },
        authorized=True,
    )
    bind_event_field(session, definition.id, f"{KEY}.value", metric.id, authorized=True)


def seed(engine):
    with Session(engine) as session, session.begin():
        owner(session)
        session.info["timezone"] = "Europe/Bratislava"
        draft = create_definition_draft(
            session, specification(10, False), actor=ACTOR, authorized=True
        )
        version = activate_definition(
            session, draft.id, draft.revision, actor=ACTOR, authorized=True
        )
        configure_metric(session, version, 10)
        first = create_custom_event(
            session, INPUTS["old_create"], actor=ACTOR, idempotency_key="original-entry"
        )
        result = first.id
        propose_definition_revision(
            session, draft.id, draft.revision, specification(5, True), actor=ACTOR, authorized=True
        )
        version = activate_definition(
            session, draft.id, draft.revision, actor=ACTOR, authorized=True
        )
        configure_metric(session, version, 5)
        return result


def write_python(engine, first):
    with Session(engine) as session, session.begin():
        session.info["timezone"] = "Europe/Bratislava"
        update_custom_event(
            session, first, INPUTS["old_edit"], revision=1, actor=ACTOR, evidence_refs=EVIDENCE
        )
        retry = create_custom_event(
            session, INPUTS["old_create"], actor=ACTOR, idempotency_key="original-entry"
        )
        assert retry.revision == 1 and retry.payload["value"] == 2
        create_custom_event(
            session,
            INPUTS["create"],
            actor=ACTOR,
            idempotency_key="new-entry",
            evidence_refs=EVIDENCE,
        )


def snapshot(engine):
    with Session(engine) as session:
        names = {event.id: event.idempotency_key for event in session.scalars(select(Event))}
        versions = {
            version.id: version.version
            for version in session.scalars(select(EventDefinitionVersion))
        }
        facts = {
            name: (
                item.kind,
                item.payload,
                item.start,
                item.end,
                item.timezone,
                item.source,
                item.status,
                item.confidence,
                item.revision,
                item.topology,
                item.validation_status,
                versions[item.definition_version_id],
                item.evidence_refs,
            )
            for item in session.scalars(select(Event))
            if (name := item.idempotency_key) is not None
        }
        audit = [
            (
                names[row.event_id],
                row.action,
                row.actor,
                row.after["payload"],
                row.after["revision"],
                row.after["evidence_refs"],
            )
            for row in session.scalars(select(Audit).order_by(Audit.id))
        ]
        metrics = sorted(
            (names[row.source_entry_id], row.value, row.unit, row.valid, row.sequence, row.timezone)
            for row in session.scalars(select(MetricObservation))
        )
        return facts, audit, metrics


def run(binary):
    python_url = os.environ["GA_TEST_DATABASE_URL"]
    rust_url = os.environ["GA_TEST_RUST_DATABASE_URL"]
    if rust_url == python_url:
        raise ValueError("Use two distinct disposable databases")
    python = isolated_engine()
    try:
        write_python(python, seed(python))
        os.environ["GA_TEST_DATABASE_URL"] = rust_url
        rust = isolated_engine()
        try:
            seed(rust)
            with TemporaryDirectory() as directory:
                path = Path(directory) / "custom-entries.json"
                path.write_text(json.dumps(INPUTS), encoding="utf-8")
                subprocess.run(
                    [binary, "--ignored"],
                    env={**os.environ, "GA_TEST_CUSTOM_ENTRIES": str(path)},
                    check=True,
                )
            for index, (expected, actual) in enumerate(
                zip(snapshot(python), snapshot(rust), strict=True)
            ):
                assert actual == expected, (index, actual, expected)
            print("Stored entries, prior version effects, exact integers and audits match Python")
        finally:
            rust.dispose()
    finally:
        os.environ["GA_TEST_DATABASE_URL"] = python_url
        python.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
