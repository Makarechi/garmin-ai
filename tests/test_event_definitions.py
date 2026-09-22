from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import DatabaseError

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import (
    CustomEntryInput,
    DefinitionSpec,
    activate_definition,
    create_custom_event,
    create_definition_draft,
    ensure_system_definitions,
    list_definitions,
    propose_definition_revision,
    retire_definition,
    update_custom_event,
    validate_stored_event,
)
from garmin_ai.events import (
    Conflict,
    EventInput,
    create_event,
    delete_event,
    deletion_response,
    undo_last,
    update_event,
)
from garmin_ai.models import Event, EventDefinition, EventDefinitionVersion
from garmin_ai.queries import list_events, timeline

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def focus_spec(*, maximum=5, topology="open_interval", key="user.focus_session"):
    return DefinitionSpec(
        key=key,
        labels={"en": "Focus session", "ru": "Фокус-сессия"},
        topology=topology,
        privacy="private",
        allowed_operations={"create", "update", "delete", "query"},
        schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "focus": {"type": "integer", "minimum": 1, "maximum": maximum},
                "distractions": {"type": "integer", "minimum": 0, "maximum": 1000},
            },
            "required": ["focus", "distractions"],
            "additionalProperties": False,
        },
        fields={
            "focus": {
                "id": f"{key}.focus",
                "labels": {"en": "Focus"},
                "semantic": "ordinal",
                "unit": "score_1-5" if maximum == 5 else "score_1-7",
            },
            "distractions": {
                "id": f"{key}.distractions",
                "labels": {"en": "Distractions"},
                "semantic": "count",
                "unit": "count",
            },
        },
    )


def activate_focus(db, **changes):
    spec = focus_spec(**changes)
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    version = activate_definition(
        db, definition.id, definition.revision, actor="test", authorized=True
    )
    return definition, version


def focus_entry(**changes):
    values = dict(
        definition_key="user.focus_session",
        start=NOW,
        timezone="UTC",
        values={"focus": 4, "distractions": 2},
        units={"focus": "score_1-5", "distractions": "count"},
    )
    values.update(changes)
    return CustomEntryInput(**values)


def test_focus_session_definition_and_entry_require_no_code_or_schema_change(db):
    with pytest.raises(PermissionError):
        create_definition_draft(db, focus_spec(), actor="test")
    definition, version = activate_focus(db)

    row = create_custom_event(db, focus_entry(), actor="test", idempotency_key="focus:1")
    replay = create_custom_event(db, focus_entry(), actor="test", idempotency_key="focus:1")

    assert row.id == replay.id
    assert row.definition_version_id == version.id
    assert row.kind == definition.key == "user.focus_session"
    assert row.topology == "open_interval"
    assert row.payload == {"type": "user.focus_session", "focus": 4, "distractions": 2}
    next_window = list_events(db, NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    assert [event["id"] for event in next_window["rows"]] == [str(row.id)]


@pytest.mark.parametrize(
    "changes",
    [
        {"values": {"focus": 4, "distractions": 2, "invented": True}},
        {"values": {"focus": 6, "distractions": 2}},
        {"units": {"focus": "percent", "distractions": "count"}},
    ],
)
def test_custom_entry_validation_rejects_unknown_fields_ranges_and_units(db, changes):
    activate_focus(db)

    with pytest.raises(ValueError):
        create_custom_event(db, focus_entry(**changes), actor="test")

    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_v1_remains_bound_and_valid_after_explicit_v2_activation(db):
    definition, version_one = activate_focus(db)
    old = create_custom_event(db, focus_entry(), actor="test")
    proposed = propose_definition_revision(
        db,
        definition.id,
        definition.revision,
        focus_spec(maximum=7),
        actor="test",
        authorized=True,
    )
    version_two = activate_definition(
        db, definition.id, proposed.revision, actor="test", authorized=True
    )

    assert version_two.version == 2
    assert old.definition_version_id == version_one.id
    assert validate_stored_event(db, old)
    with pytest.raises(ValueError):
        update_custom_event(
            db,
            old.id,
            focus_entry(
                values={"focus": 6, "distractions": 2},
                units={"focus": "score_1-7", "distractions": "count"},
            ),
            revision=old.revision,
            actor="test",
        )
    new = create_custom_event(
        db,
        focus_entry(
            start=NOW + timedelta(hours=1),
            values={"focus": 6, "distractions": 1},
            units={"focus": "score_1-7", "distractions": "count"},
        ),
        actor="test",
    )
    assert new.definition_version_id == version_two.id


def test_point_custom_entry_does_not_leak_into_later_query_window(db):
    activate_focus(db, topology="point", key="user.focus_check")
    row = create_custom_event(
        db,
        CustomEntryInput(
            definition_key="user.focus_check",
            start=NOW,
            timezone="UTC",
            values={"focus": 4, "distractions": 0},
            units={"focus": "score_1-5", "distractions": "count"},
        ),
        actor="test",
    )

    assert row.topology == "point"
    result = list_events(db, NOW + timedelta(minutes=1), NOW + timedelta(hours=1))
    assert result["rows"] == []


def test_external_refs_and_executable_schema_features_are_rejected():
    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["properties"]["focus"] = {"$ref": "https://example.invalid/schema"}
    with pytest.raises(ValueError, match="local"):
        DefinitionSpec.model_validate(invalid)
    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["properties"]["focus"]["pattern"] = ".*"
    with pytest.raises(ValueError, match="Unsupported"):
        DefinitionSpec.model_validate(invalid)
    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["$defs"] = {"loop": {"$ref": "#/$defs/loop"}}
    with pytest.raises(ValueError, match="Recursive"):
        DefinitionSpec.model_validate(invalid)
    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["properties"]["type"] = {
        "type": "string",
        "maxLength": 20,
    }
    invalid["fields"]["type"] = {
        "id": "user.focus_session.type",
        "labels": {"en": "Type"},
        "semantic": "text",
        "unit": None,
    }
    with pytest.raises(ValueError, match="reserved"):
        DefinitionSpec.model_validate(invalid)
    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["properties"]["nested"] = {
        "properties": {"value": {"type": "string", "maxLength": 20}},
        "required": ["value"],
        "additionalProperties": False,
    }
    invalid["fields"]["nested"] = {
        "id": "user.focus_session.nested",
        "labels": {"en": "Nested"},
        "semantic": "text",
        "unit": None,
    }
    with pytest.raises(ValueError, match="type object"):
        DefinitionSpec.model_validate(invalid)

    for malformed in (["integer", "null"], {"unexpected": "shape"}):
        invalid = focus_spec().model_dump(mode="json", by_alias=True)
        invalid["schema"]["properties"]["focus"]["type"] = malformed
        with pytest.raises(ValueError, match="schema type"):
            DefinitionSpec.model_validate(invalid)

    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["required"] = [["focus"]]
    with pytest.raises(ValueError, match="required"):
        DefinitionSpec.model_validate(invalid)


@pytest.mark.parametrize("property_schema", [{}, {"title": "Focus"}])
def test_unconstrained_custom_field_is_rejected(property_schema):
    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["properties"]["focus"] = property_schema
    with pytest.raises(ValueError, match="explicit type or constraint"):
        DefinitionSpec.model_validate(invalid)


@pytest.mark.parametrize("keyword", ["enum", "const"])
def test_definition_rejects_literal_that_entry_validation_cannot_store(keyword):
    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["properties"]["focus"] = {
        "type": "string",
        keyword: ["x" * 20_000] if keyword == "enum" else "x" * 20_000,
    }
    with pytest.raises(ValueError, match="Entry string is too long"):
        DefinitionSpec.model_validate(invalid)


@pytest.mark.parametrize(
    ("field_schema", "expected"),
    [
        ({"type": "integer", "minimum": 0, "maximum": 10, "const": "x"}, "literal"),
        ({"type": "integer", "minimum": 0, "maximum": 10, "enum": [11]}, "literal"),
        ({"type": "integer", "minimum": 0, "maximum": 10**400}, "finite"),
    ],
)
def test_definition_rejects_impossible_literals_and_huge_bounds(field_schema, expected):
    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["properties"]["focus"] = field_schema
    with pytest.raises(ValueError, match=expected):
        DefinitionSpec.model_validate(invalid)


def test_literal_data_is_not_scanned_for_schema_references():
    from garmin_ai.definitions import validate_schema

    schema = {
        "type": "object",
        "properties": {"focus": {"const": {"$ref": 1}}},
        "required": ["focus"],
        "additionalProperties": False,
    }
    validate_schema(schema)


def test_system_cross_field_rules_are_checked_in_discovery_and_stored_rows(db):
    from jsonschema import Draft202012Validator

    versions = ensure_system_definitions(db)
    wellbeing = versions["wellbeing_observation"]
    assert not Draft202012Validator(wellbeing.schema).is_valid({"type": "wellbeing_observation"})
    assert not Draft202012Validator(wellbeing.schema).is_valid(
        {"type": "wellbeing_observation", "notes": "   "}
    )
    caffeine = versions["caffeine"]
    assert caffeine.schema["x-server-validation"]["model"] == "Caffeine"

    row = create_event(
        db,
        EventInput(start=NOW, payload={"type": "caffeine", "beverage": "synthetic"}),
        actor="test",
    )
    row.payload = {
        "type": "caffeine",
        "beverage": "synthetic",
        "caffeine_mg_min": 200,
        "caffeine_mg_max": 100,
    }
    with pytest.raises(ValueError, match="validation model"):
        validate_stored_event(db, row)


def test_schema_reference_expansion_is_bounded():
    invalid = focus_spec().model_dump(mode="json", by_alias=True)
    invalid["schema"]["$defs"] = {
        f"level{number}": (
            {"$ref": f"#/$defs/level{number + 1}"}
            if number < 10
            else {"type": "integer", "minimum": 1, "maximum": 5}
        )
        for number in range(11)
    }
    invalid["schema"]["properties"]["focus"] = {"$ref": "#/$defs/level0"}

    with pytest.raises(ValueError, match="Expanded schema"):
        DefinitionSpec.model_validate(invalid)


def test_system_pydantic_definition_is_registered_and_historical_rows_backfill(db):
    row = create_event(db, EventInput(start=NOW, payload={"type": "migraine"}), actor="test")
    definition = db.scalar(select(EventDefinition).where(EventDefinition.key == "system.migraine"))
    version = db.get(EventDefinitionVersion, row.definition_version_id)
    assert definition.namespace == "system" and version.definition_id == definition.id

    row.definition_version_id = None
    db.flush()
    ensure_system_definitions(db, backfill=True)
    db.refresh(row)
    assert row.definition_version_id == version.id
    assert validate_stored_event(db, row)


def test_backfill_leaves_rows_outside_the_current_contract_unbound(db):
    from uuid import uuid4

    row = Event(
        kind="symptom_observation",
        start=NOW,
        timezone="UTC",
        source="manual",
        payload={
            "type": "symptom_observation",
            "episode_id": str(uuid4()),
            "impact": "   ",
        },
        topology="point",
    )
    db.add(row)
    db.flush()

    ensure_system_definitions(db, backfill=True)
    db.refresh(row)

    assert row.definition_version_id is None


def test_symptom_impact_must_match_published_nonblank_contract():
    from uuid import uuid4

    from garmin_ai.events import SymptomObservation

    with pytest.raises(ValueError, match="Symptom impact cannot be blank"):
        SymptomObservation(episode_id=uuid4(), impact="   ")


def test_definition_discovery_exposes_active_immutable_contract(db):
    definition, version = activate_focus(db)

    discovered = next(row for row in list_definitions(db) if row["id"] == str(definition.id))

    assert discovered["contract"]["id"] == str(version.id)
    assert discovered["contract"]["schema"] == version.schema
    assert discovered["contract"]["fields"] == version.field_metadata
    assert "query" in discovered["contract"]["allowed_operations"]


def test_discovery_resolves_retired_and_historical_contracts(db):
    definition, first = activate_focus(db)
    proposal = propose_definition_revision(
        db,
        definition.id,
        definition.revision,
        focus_spec(maximum=7),
        actor="test",
        authorized=True,
    )
    second = activate_definition(
        db, definition.id, proposal.revision, actor="test", authorized=True
    )
    retire_definition(db, definition.id, definition.revision, authorized=True)

    found = next(
        row for row in list_definitions(db, include_retired=True) if row["id"] == str(definition.id)
    )
    assert found["status"] == "retired"
    assert [item["id"] for item in found["versions"]] == [str(first.id), str(second.id)]
    latest_page = list_definitions(
        db, include_retired=True, definition_key=definition.key, versions_limit=1
    )[0]
    assert [item["id"] for item in latest_page["versions"]] == [str(second.id)]
    assert latest_page["versions_before"] == second.version
    earlier_page = list_definitions(
        db,
        include_retired=True,
        definition_key=definition.key,
        before_version=latest_page["versions_before"],
        versions_limit=1,
    )[0]
    assert [item["id"] for item in earlier_page["versions"]] == [str(first.id)]


def test_array_keywords_require_array_type():
    spec = focus_spec().model_dump(mode="json", by_alias=True)
    spec["schema"]["properties"]["focus"] = {
        "items": {"type": "integer", "minimum": 1, "maximum": 5},
        "maxItems": 2,
    }
    with pytest.raises(ValueError, match="Array schema keywords"):
        DefinitionSpec.model_validate(spec)


def test_system_contracts_have_kind_and_field_semantics(db):
    versions = ensure_system_definitions(db)
    assert versions["meal"].schema["properties"]["type"]["const"] == "meal"
    assert versions["migraine"].field_metadata["severity"]["semantic"] == "ordinal"
    assert versions["migraine"].field_metadata["aura"]["semantic"] == "boolean"
    assert versions["caffeine"].field_metadata["caffeine_mg_estimate"]["unit"] == "mg"


def test_mcp_startup_bootstraps_system_definitions(db, db_engine):
    from garmin_ai.mcp_server import initialize_identity

    db.commit()
    initialize_identity(db_engine, Settings())
    db.expire_all()
    assert (
        db.scalar(select(EventDefinition.id).where(EventDefinition.key == "system.migraine"))
        is not None
    )


def test_deletion_of_nonqueryable_entry_returns_only_acknowledgement(db):
    spec = focus_spec()
    spec.allowed_operations = {"create", "delete"}
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    activate_definition(db, definition.id, definition.revision, actor="test", authorized=True)
    row = create_custom_event(db, focus_entry(), actor="test")
    result = deletion_response(db, delete_event(db, row.id, revision=row.revision, actor="test"))
    assert result == {"id": str(row.id), "revision": 2, "deleted": True}


def test_definition_versions_are_database_immutable(db):
    _, version = activate_focus(db)
    with pytest.raises(DatabaseError), db.begin_nested():
        version.privacy = "sensitive"
        db.flush()


def test_definition_operations_are_enforced_for_existing_entries(db):
    spec = focus_spec()
    spec.allowed_operations = {"create", "query"}
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    activate_definition(db, definition.id, definition.revision, actor="test", authorized=True)
    row = create_custom_event(db, focus_entry(), actor="test")

    with pytest.raises(PermissionError, match="updates"):
        update_custom_event(db, row.id, focus_entry(), revision=row.revision, actor="test")
    with pytest.raises(PermissionError, match="deletion"):
        delete_event(db, row.id, revision=row.revision, actor="test")
    assert not row.deleted


def test_idempotent_replay_uses_original_version_after_revision_and_retirement(db):
    definition, version_one = activate_focus(db)
    row = create_custom_event(db, focus_entry(), actor="test", idempotency_key="focus:stable")
    proposed = propose_definition_revision(
        db,
        definition.id,
        definition.revision,
        focus_spec(maximum=7),
        actor="test",
        authorized=True,
    )

    # A proposal does not suspend the current active version.
    while_proposed = create_custom_event(
        db,
        focus_entry(start=NOW + timedelta(hours=1)),
        actor="test",
    )
    assert while_proposed.definition_version_id == version_one.id
    activate_definition(db, definition.id, proposed.revision, actor="test", authorized=True)
    assert (
        create_custom_event(db, focus_entry(), actor="test", idempotency_key="focus:stable").id
        == row.id
    )
    retire_definition(db, definition.id, definition.revision, authorized=True)
    assert (
        create_custom_event(db, focus_entry(), actor="test", idempotency_key="focus:stable").id
        == row.id
    )
    with pytest.raises(Conflict):
        create_custom_event(
            db,
            focus_entry(values={"focus": 3, "distractions": 2}),
            actor="test",
            idempotency_key="focus:stable",
        )


def test_nonqueryable_idempotent_replay_returns_original_creation_snapshot(db):
    spec = focus_spec()
    spec.allowed_operations = {"create", "update"}
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    activate_definition(db, definition.id, definition.revision, actor="test", authorized=True)
    row = create_custom_event(db, focus_entry(), actor="test", idempotency_key="focus:private")
    update_custom_event(
        db,
        row.id,
        focus_entry(values={"focus": 2, "distractions": 1}),
        revision=row.revision,
        actor="test",
    )

    replay = create_custom_event(db, focus_entry(), actor="test", idempotency_key="focus:private")
    assert replay.id == row.id
    assert replay.revision == 1
    assert replay.payload == {"type": "user.focus_session", "focus": 4, "distractions": 2}
    assert row.payload["focus"] == 2


def test_nonqueryable_custom_entries_are_hidden_and_policy_denials_are_403(db, db_engine):
    from garmin_ai.agent import context_for, interpret

    spec = focus_spec()
    spec.allowed_operations = {"create", "update", "delete"}
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    activate_definition(db, definition.id, definition.revision, actor="test", authorized=True)
    row = create_custom_event(db, focus_entry(), actor="test")
    db.commit()

    assert list_events(db, NOW - timedelta(minutes=1), NOW + timedelta(hours=1))["rows"] == []
    layers = timeline(db, NOW - timedelta(minutes=1), NOW + timedelta(hours=1))["layers"]
    assert all(not values for values in layers.values())
    assert str(row.id) not in {event["id"] for event in context_for(db, NOW)["recent_events"]}

    class ProviderMustNotReceiveHiddenEvent:
        def structured(self, *_args, **_kwargs):
            pytest.fail("A non-queryable entry must not reach the model")

    hidden = interpret(
        db,
        ProviderMustNotReceiveHiddenEvent(),
        f"inspect {row.id}",
        Settings(timezone="UTC"),
        NOW,
    )
    assert hidden.intent == "clarify"
    key = "query-key-" + "x" * 32
    client = TestClient(
        create_app(
            Settings(api_tokens=[ApiToken(key=key, scopes={"read:diary", "write:diary"})]),
            db_engine,
        )
    )
    headers = {"Authorization": "Bearer " + key}
    assert client.get(f"/events/{row.id}", headers=headers).status_code == 404

    allowed = focus_spec(key="user.no_create")
    allowed.allowed_operations = {"query"}
    no_create = create_definition_draft(db, allowed, actor="test", authorized=True)
    activate_definition(db, no_create.id, no_create.revision, actor="test", authorized=True)
    db.commit()
    body = focus_entry(definition_key="user.no_create").model_dump(mode="json")
    assert client.post("/entries", json=body, headers=headers).status_code == 403


def test_old_custom_open_interval_remains_in_model_context(db):
    from garmin_ai.agent import context_for

    activate_focus(db)
    row = create_custom_event(
        db,
        focus_entry(start=NOW - timedelta(days=30)),
        actor="test",
    )

    assert str(row.id) in {event["id"] for event in context_for(db, NOW)["recent_events"]}


def test_reintroduced_field_keeps_identity_from_all_prior_versions(db):
    definition, _ = activate_focus(db)
    without_focus = focus_spec()
    without_focus.payload_schema["properties"].pop("focus")
    without_focus.payload_schema["required"].remove("focus")
    without_focus.fields.pop("focus")
    proposed = propose_definition_revision(
        db,
        definition.id,
        definition.revision,
        without_focus,
        actor="test",
        authorized=True,
    )
    activate_definition(db, definition.id, proposed.revision, actor="test", authorized=True)
    reintroduced = focus_spec()
    reintroduced.fields["focus"].id = "user.focus_session.reintroduced"

    with pytest.raises(ValueError, match="identities"):
        propose_definition_revision(
            db,
            definition.id,
            definition.revision,
            reintroduced,
            actor="test",
            authorized=True,
        )


def test_all_system_contracts_require_payload_discriminator(db):
    from garmin_ai.definitions import ensure_system_definitions

    versions = ensure_system_definitions(db)
    assert versions
    assert all("type" in version.schema["required"] for version in versions.values())


def test_system_definition_key_filters_legacy_stored_kind(db):
    event = create_event(
        db,
        EventInput(start=NOW, payload={"type": "migraine"}),
        actor="test",
    )

    rows = list_events(
        db,
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=1),
        kind="system.migraine",
    )["rows"]

    assert [row["id"] for row in rows] == [str(event.id)]


@pytest.mark.parametrize(
    "operations,delete_visible",
    [({"create", "query"}, False), ({"create", "delete", "query"}, True)],
)
def test_custom_history_only_offers_supported_implemented_actions(db, operations, delete_visible):
    from garmin_ai.telegram_history import history_page

    spec = focus_spec()
    spec.allowed_operations = operations
    definition = create_definition_draft(db, spec, actor="test", authorized=True)
    activate_definition(db, definition.id, definition.revision, actor="test", authorized=True)
    create_custom_event(db, focus_entry(), actor="test")

    history_page(db, NOW + timedelta(minutes=1))
    labels = [
        button["text"] for row in db.info["reply_keyboard"]["inline_keyboard"] for button in row
    ]

    assert not any("Исправить" in label for label in labels)
    assert any("Удалить" in label for label in labels) is delete_visible


def test_builtin_update_path_cannot_replace_custom_definition(db):
    activate_focus(db)
    row = create_custom_event(db, focus_entry(), actor="test")

    with pytest.raises(ValueError, match="custom correction"):
        update_event(
            db,
            row.id,
            EventInput(start=NOW, payload={"type": "migraine"}),
            revision=row.revision,
            actor="test",
        )
    assert row.kind == "user.focus_session"


def test_same_kind_system_correction_rebinds_to_current_version(db, monkeypatch):
    import garmin_ai.definitions

    row = create_event(db, EventInput(start=NOW, payload={"type": "migraine"}), actor="test")
    version_one = db.get(EventDefinitionVersion, row.definition_version_id)
    definition = db.get(EventDefinition, version_one.definition_id)
    version_two = EventDefinitionVersion(
        definition_id=definition.id,
        version=2,
        schema=version_one.schema,
        schema_hash=version_one.schema_hash,
        topology=version_one.topology,
        field_metadata=version_one.field_metadata,
        labels=version_one.labels,
        privacy=version_one.privacy,
        allowed_operations=version_one.allowed_operations,
    )
    db.add(version_two)
    definition.current_version = 2
    db.flush()
    monkeypatch.setattr(
        garmin_ai.definitions, "ensure_system_definition", lambda session, kind: version_two
    )

    update_event(
        db,
        row.id,
        EventInput(start=NOW, end=NOW + timedelta(hours=1), payload={"type": "migraine"}),
        revision=row.revision,
        actor="test",
    )

    assert row.definition_version_id == version_two.id


def test_retired_definition_cannot_accept_an_unactivatable_revision(db):
    definition, _ = activate_focus(db)
    retire_definition(db, definition.id, definition.revision, authorized=True)

    with pytest.raises(ValueError, match="Retired"):
        propose_definition_revision(
            db,
            definition.id,
            definition.revision,
            focus_spec(maximum=7),
            actor="test",
            authorized=True,
        )

    assert definition.draft is None and definition.status == "retired"


def test_undo_restores_definition_binding_and_open_topology(db):
    row = create_event(db, EventInput(start=NOW, payload={"type": "migraine"}), actor="test")
    original_version = row.definition_version_id
    update_event(
        db,
        row.id,
        EventInput(
            start=NOW,
            end=NOW + timedelta(hours=1),
            payload={"type": "migraine"},
        ),
        revision=row.revision,
        actor="test",
    )
    assert row.topology == "bounded_interval"

    undo_last(db, actor="test")

    assert row.end is None
    assert row.topology == "open_interval"
    assert row.definition_version_id == original_version


def test_undo_restores_payload_under_its_historical_system_contract(db):
    row = create_event(
        db, EventInput(start=NOW, payload={"type": "note", "description": "old"}), actor="test"
    )
    current = db.get(EventDefinitionVersion, row.definition_version_id)
    definition = db.get(EventDefinition, current.definition_id)
    old_schema = deepcopy(current.schema)
    old_schema["properties"]["legacy_label"] = {"type": "string"}
    historical = EventDefinitionVersion(
        definition_id=definition.id,
        version=current.version + 1,
        schema=old_schema,
        schema_hash="historical-note-with-legacy-label",
        topology=current.topology,
        field_metadata=current.field_metadata,
        labels=current.labels,
        privacy=current.privacy,
        allowed_operations=current.allowed_operations,
    )
    db.add(historical)
    db.flush()
    definition.current_version = historical.version
    row.definition_version_id = historical.id
    row.payload = {**row.payload, "legacy_label": "retained"}
    db.flush()

    update_event(
        db,
        row.id,
        EventInput(start=NOW, payload={"type": "note", "description": "new"}),
        revision=row.revision,
        actor="test",
    )
    assert definition.current_version > historical.version

    undo_last(db, actor="test")
    assert row.payload["legacy_label"] == "retained"
    assert row.definition_version_id == historical.id


def test_undo_validates_and_restores_custom_entry_version(db):
    _, version = activate_focus(db)
    row = create_custom_event(db, focus_entry(), actor="test")
    update_custom_event(
        db,
        row.id,
        focus_entry(values={"focus": 2, "distractions": 1}),
        revision=row.revision,
        actor="test",
    )

    undo_last(db, actor="test")

    assert row.payload["focus"] == 4
    assert row.definition_version_id == version.id
    assert row.topology == "open_interval"


def test_api_uses_separate_definition_permission_and_shared_entry_validation(db, db_engine):
    key = "definition-key-" + "x" * 32
    diary = "diary-key-" + "x" * 32
    manager = "manager-key-" + "x" * 32
    client = TestClient(
        create_app(
            Settings(
                api_tokens=[
                    ApiToken(key=key, scopes={"manage:definitions", "read:diary"}),
                    ApiToken(key=diary, scopes={"read:diary", "write:diary"}),
                    ApiToken(key=manager, scopes={"manage:definitions"}),
                ]
            ),
            db_engine,
        )
    )
    spec = focus_spec().model_dump(mode="json", by_alias=True)
    assert (
        client.post(
            "/definitions", json=spec, headers={"Authorization": "Bearer " + diary}
        ).status_code
        == 403
    )
    created = client.post("/definitions", json=spec, headers={"Authorization": "Bearer " + key})
    assert created.status_code == 200
    discovered = client.get(
        "/definitions",
        params={"definition_key": spec["key"]},
        headers={"Authorization": "Bearer " + manager},
    )
    assert discovered.status_code == 200
    assert any(row["id"] == created.json()["id"] for row in discovered.json())
    activated = client.post(
        f"/definitions/{created.json()['id']}/activate",
        json={"revision": created.json()["revision"]},
        headers={"Authorization": "Bearer " + key},
    )
    assert activated.status_code == 200
    invalid_entries = []
    unknown = focus_entry().model_dump(mode="json")
    unknown["values"]["invented"] = True
    invalid_entries.append(unknown)
    out_of_range = focus_entry().model_dump(mode="json")
    out_of_range["values"]["focus"] = 6
    invalid_entries.append(out_of_range)
    wrong_unit = focus_entry().model_dump(mode="json")
    wrong_unit["units"]["focus"] = "percent"
    invalid_entries.append(wrong_unit)
    for entry in invalid_entries:
        assert (
            client.post(
                "/entries", json=entry, headers={"Authorization": "Bearer " + diary}
            ).status_code
            == 422
        )


def test_api_health_stays_available_before_definition_migration(db_engine):
    isolated = create_engine(
        db_engine.url,
        connect_args={"options": "-c search_path=pg_catalog"},
        hide_parameters=True,
    )
    try:
        with TestClient(create_app(Settings(), isolated)) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 503
    finally:
        isolated.dispose()
