from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from garmin_ai.canonical_events import backfill_canonical_events
from garmin_ai.definitions import (
    CustomEntryInput,
    DefinitionSpec,
    activate_definition,
    create_custom_event,
    create_definition_draft,
)
from garmin_ai.events import EventInput, create_event, serialize_event, undo_last, update_event
from garmin_ai.models import Audit, Event

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def test_server_derives_separate_provenance_and_compatible_envelope(db):
    event = create_event(
        db,
        EventInput(
            start=NOW,
            source="telegram_voice",
            payload={"type": "medication", "name": "test", "dose": None, "unit": None},
        ),
        actor="telegram:synthetic-owner",
    )
    db.flush()

    encoded = serialize_event(event)
    assert encoded["id"] == str(event.id)
    assert encoded["payload"]["dose"] is None
    assert encoded["end"] is None
    assert encoded["source"] == "telegram_voice"
    assert encoded["canonical"] == {
        "version": 1,
        "definition_version_id": str(event.definition_version_id),
        "observed": {
            "start": NOW.isoformat(),
            "end": None,
            "timezone": "Europe/Bratislava",
            "topology": "point",
            "precision": "instant",
        },
        "recorded_at": event.recorded_at.isoformat(),
        "ingested_at": event.ingested_at.isoformat(),
        "provenance": {
            "assertion_kind": "user_report",
            "producer": "telegram",
            "transport": "telegram_voice",
            "author": "owner",
            "evidence_refs": [],
            "confidence": 1.0,
            "validation_status": "schema_validated",
        },
    }


@pytest.mark.parametrize(
    (
        "source",
        "status",
        "actor",
        "assertion",
        "producer",
        "transport",
        "author",
        "validation",
    ),
    [
        (
            "manual",
            "confirmed",
            "api",
            "user_report",
            "owner",
            "api",
            "owner",
            "schema_validated",
        ),
        (
            "mcp",
            "confirmed",
            "mcp",
            "user_report",
            "owner",
            "mcp",
            "owner",
            "schema_validated",
        ),
        (
            "wearable",
            "confirmed",
            "wearable:synthetic-device",
            "device_measurement",
            "wearable",
            "connector",
            None,
            "trusted",
        ),
        (
            "inferred",
            "inferred",
            "telegram:synthetic-owner",
            "user_report",
            "telegram",
            "telegram",
            "owner",
            "needs_confirmation",
        ),
        (
            "inferred",
            "inferred",
            "api",
            "user_report",
            "owner",
            "api",
            "owner",
            "needs_confirmation",
        ),
    ],
)
def test_provenance_dimensions_do_not_conflate_transport_and_assertion(
    db, source, status, actor, assertion, producer, transport, author, validation
):
    row = create_event(
        db,
        EventInput(
            start=NOW,
            source=source,
            status=status,
            payload={"type": "note", "description": "synthetic"},
        ),
        actor=actor,
    )
    assert (
        row.assertion_kind,
        row.producer,
        row.transport,
        row.author,
        row.validation_status,
    ) == (assertion, producer, transport, author, validation)


def test_client_source_cannot_forge_a_trusted_device_producer(db):
    row = create_event(
        db,
        EventInput(
            start=NOW,
            source="wearable",
            payload={"type": "note", "description": "claimed device source"},
        ),
        actor="api",
    )

    assert row.assertion_kind == "user_report"
    assert row.producer == "owner" and row.transport == "api"
    assert row.validation_status == "schema_validated"


def test_update_and_undo_restore_canonical_metadata(db):
    row = create_event(
        db,
        EventInput(start=NOW, payload={"type": "migraine"}),
        actor="owner",
    )
    db.flush()
    before = serialize_event(row)
    update_event(
        db,
        row.id,
        EventInput(
            start=NOW,
            end=NOW + timedelta(hours=1),
            source="telegram_text",
            payload={"type": "migraine"},
        ),
        revision=row.revision,
        actor="owner",
    )
    assert row.time_precision == "interval" and row.transport == "telegram_text"

    undo_last(db, actor="owner")
    restored = serialize_event(row)
    assert restored["canonical"] == before["canonical"]
    assert restored["id"] == before["id"] and restored["revision"] == before["revision"] + 2


def test_undo_reconstructs_metadata_from_legacy_audit(db):
    row = create_event(
        db,
        EventInput(start=NOW, source="manual", payload={"type": "note", "description": "x"}),
        actor="owner",
    )
    update_event(
        db,
        row.id,
        EventInput(
            start=NOW,
            end=NOW + timedelta(hours=1),
            source="telegram_text",
            payload={"type": "note", "description": "x"},
        ),
        revision=row.revision,
        actor="owner",
    )
    audit = db.scalar(select(Audit).order_by(Audit.id.desc()).limit(1))
    legacy_keys = {
        "envelope_version",
        "time_precision",
        "assertion_kind",
        "producer",
        "transport",
        "author",
        "evidence_refs",
        "validation_status",
        "recorded_at",
        "ingested_at",
        "topology",
    }
    audit.before = {key: value for key, value in audit.before.items() if key not in legacy_keys}
    db.flush()

    undo_last(db, actor="owner")
    assert row.source == "manual"
    assert row.producer == "owner"
    assert row.transport == "manual"
    assert row.time_precision == "instant"


def test_backfill_validation_is_repeatable_and_has_no_audit_effects(db):
    row = create_event(
        db,
        EventInput(start=NOW, payload={"type": "note", "description": "synthetic"}),
        actor="owner",
    )
    db.flush()
    audit_count = db.scalar(select(func.count()).select_from(Audit))

    assert backfill_canonical_events(db) == 1
    assert backfill_canonical_events(db) == 1
    assert db.scalar(select(func.count()).select_from(Audit)) == audit_count

    row.definition_version_id = None
    db.flush()
    with pytest.raises(ValueError, match="unresolved definitions"):
        backfill_canonical_events(db)


def test_custom_tracker_uses_same_envelope_without_database_change(db):
    spec = DefinitionSpec(
        key="user.focus",
        labels={"en": "Focus"},
        topology="bounded_interval",
        schema={
            "type": "object",
            "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 5}},
            "required": ["score"],
            "additionalProperties": False,
        },
        fields={
            "score": {
                "id": "user.focus.score",
                "labels": {"en": "Score"},
                "semantic": "ordinal",
                "unit": "score_1-5",
            }
        },
    )
    definition = create_definition_draft(db, spec, actor="owner", authorized=True)
    version = activate_definition(
        db, definition.id, definition.revision, actor="owner", authorized=True
    )
    row = create_custom_event(
        db,
        CustomEntryInput(
            definition_key="user.focus",
            start=NOW,
            end=NOW + timedelta(minutes=25),
            timezone="UTC",
            source="mcp",
            values={"score": 4},
            units={"score": "score_1-5"},
        ),
        actor="mcp",
    )

    assert row.definition_version_id == version.id
    assert row.time_precision == "interval"
    assert row.assertion_kind == "user_report" and row.transport == "mcp"
    assert db.scalar(select(func.count()).select_from(Event)) == 1
