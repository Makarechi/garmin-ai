"""Cross-check deletion and grouped undo with entries written by pinned Python."""

import argparse
import subprocess
from pathlib import Path
from uuid import uuid4

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.definitions import activate_definition, create_custom_event, create_definition_draft
from garmin_ai.events import EventInput, create_event, update_event
from garmin_ai.metric_definitions import bind_event_field, register_metric_definition
from garmin_ai.models import Audit, Event, MetricObservation

START = "2026-10-24T23:30:00+00:00"


def note(session, actor, label, operation=None):
    return create_event(
        session,
        EventInput.model_validate(
            {"start": START, "payload": {"type": "note", "description": label}}
        ),
        actor=actor,
        idempotency_key=label,
        operation_id=operation,
    )


def private_entry(session, key, allowed):
    definition = create_definition_draft(
        session,
        {
            "key": key,
            "labels": {"en": "Synthetic private entry"},
            "topology": "point",
            "allowed_operations": allowed,
            "schema": {
                "type": "object",
                "properties": {"sample": {"$ref": "#/$defs/synthetic"}},
                "required": ["sample"],
                "additionalProperties": False,
                "$defs": {"synthetic": {"type": "integer", "minimum": 1, "maximum": 10}},
            },
            "fields": {
                "sample": {
                    "id": key + ".sample",
                    "labels": {"en": "Sample"},
                    "semantic": "count",
                    "unit": "count",
                }
            },
        },
        actor="synthetic-definition",
        authorized=True,
    )
    version = activate_definition(
        session,
        definition.id,
        definition.revision,
        actor="synthetic-definition",
        authorized=True,
    )
    if "delete" in allowed:
        metric = register_metric_definition(
            session,
            {
                "key": "user.synthetic_hidden_count",
                "labels": {"en": "Synthetic count"},
                "value_kind": "physical_number",
                "unit": "count",
                "dimension": "count",
                "aggregation": "latest",
                "allowed_methods": ["latest"],
                "coverage": {"kind": "all_values"},
                "time_semantics": "point",
                "minimum": 1,
                "maximum": 10,
            },
            authorized=True,
        )
        bind_event_field(session, version.id, key + ".sample", metric.id, authorized=True)
    return create_custom_event(
        session,
        {"definition_key": key, "start": START, "values": {"sample": 2}},
        actor="synthetic-import",
        idempotency_key=key,
    ).id


def run(binary):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            group = uuid4()
            episode = create_event(
                session,
                EventInput.model_validate({"start": START, "payload": {"type": "migraine"}}),
                actor="telegram:synthetic-group",
                idempotency_key="synthetic-migraine-group",
                operation_id=group,
            )
            symptom = create_event(
                session,
                EventInput.model_validate(
                    {
                        "start": START,
                        "payload": {
                            "type": "symptom_observation",
                            "episode_id": str(episode.id),
                            "severity": 4,
                        },
                    }
                ),
                actor="telegram:synthetic-group",
                idempotency_key="synthetic-symptom-group",
                operation_id=group,
            )
            outsider = note(session, "api", "synthetic-outsider", group)

            conflict = uuid4()
            first = note(session, "telegram:synthetic-stale", "synthetic-stale-first", conflict)
            second = note(session, "telegram:synthetic-stale", "synthetic-stale-second", conflict)
            update_event(
                session,
                first.id,
                EventInput.model_validate(
                    {"start": START, "payload": {"type": "note", "description": "newer change"}}
                ),
                revision=1,
                actor="mcp",
            )

            legacy = note(session, "telegram:synthetic-creator", "synthetic-legacy-envelope")
            update_event(
                session,
                legacy.id,
                EventInput.model_validate(
                    {"start": START, "payload": {"type": "note", "description": "recent edit"}}
                ),
                revision=1,
                actor="mcp",
            )
            session.flush()
            audit = session.scalar(
                select(Audit).where(Audit.event_id == legacy.id, Audit.action == "update")
            )
            audit.before = {
                key: value
                for key, value in audit.before.items()
                if key
                in {
                    "kind",
                    "start",
                    "end",
                    "timezone",
                    "source",
                    "confidence",
                    "status",
                    "original_text",
                    "payload",
                    "deleted",
                    "created_at",
                }
            }
            private = private_entry(session, "user.synthetic_hidden", ["create", "delete"])
            forbidden = private_entry(session, "user.synthetic_keep", ["create", "query"])
            ids = [episode.id, symptom.id, outsider.id, second.id, legacy.id]

        subprocess.run([binary, "--ignored"], check=True)
        with Session(engine) as session, session.begin():
            flags = [session.get(Event, event_id).deleted for event_id in ids]
            assert flags == [True, True, False, False, False]
            restored = session.get(Event, ids[4])
            assert restored.payload["description"] == "synthetic-legacy-envelope"
            assert restored.producer == "telegram"
            assert restored.recorded_at == restored.created_at
            assert restored.recorded_at == restored.ingested_at
            assert session.get(Event, private).revision == 3
            assert session.get(Event, forbidden).revision == 1
            observations = session.scalars(
                select(MetricObservation)
                .where(MetricObservation.source_entry_id == private)
                .order_by(MetricObservation.projection_version)
            ).all()
            assert [(fact.value, fact.valid) for fact in observations] == [(2, False), (2, True)]
            group_audits = session.scalars(
                select(Audit)
                .where(Audit.operation_id == group, Audit.action == "undo")
                .order_by(Audit.id)
            ).all()
            assert [audit.event_id for audit in group_audits] == [ids[1], ids[0]]
            assert all(audit.after["deleted"] for audit in group_audits)
            print("Pinned Python verified restored legacy/custom entries and grouped Rust audits")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
