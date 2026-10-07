"""Community packs create only owner-confirmed, isolated tracker definitions."""

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from garmin_ai.api import create_app
from garmin_ai.community_packs import (
    CommunityPack,
    PackConfirmation,
    catalog,
    import_community_pack,
    preview_community_pack,
)
from garmin_ai.config import ApiToken, Settings
from garmin_ai.events import Conflict
from garmin_ai.models import EventDefinition, TrackerConfig
from garmin_ai.scenario_packs import ensure_scenario_packs


def confirm(db, pack):
    preview = preview_community_pack(db, pack)
    return import_community_pack(
        db,
        PackConfirmation(pack=pack, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )


def test_three_catalog_packs_install_without_integrations_and_reimport_safely(db):
    for pack in catalog():
        preview = preview_community_pack(db, pack)
        assert preview["changes"] == [
            {"definition_key": "user." + item["key"], "status": "create"}
            for item in pack["trackers"]
        ]
        assert preview["permissions"] == {
            "create_trackers": True,
            "enable_reminders": False,
            "enable_external_sources": False,
            "grant_model_or_channel_access": False,
        }
        result = import_community_pack(
            db,
            PackConfirmation(pack=pack, confirmation_token=preview["confirmation_token"]),
            actor="test",
        )
        assert len(result["created"]) == 1
        assert result["created"][0]["tracker"]["reminder_enabled"] is False
        repeated = confirm(db, pack)
        assert repeated["created"] == []
        assert repeated["changes"][0]["status"] == "already_installed"

    definitions = db.scalars(
        select(EventDefinition).where(EventDefinition.namespace == "user")
    ).all()
    trackers = db.scalars(select(TrackerConfig)).all()
    assert len(definitions) == len(trackers) == 3
    assert {row.current_version for row in definitions} == {1}
    assert not any(row.reminder_enabled for row in trackers)


def test_conflicting_pack_cannot_replace_another_definition_or_old_version(db):
    first = catalog()[0]
    confirm(db, first)
    original = db.scalar(select(EventDefinition).where(EventDefinition.key == "user.daily_energy"))
    original_id, original_version = original.id, original.current_version

    changed = deepcopy(first)
    changed["version"] = 2
    changed["trackers"][0]["fields"][0]["label"] = "Different scale"
    preview = preview_community_pack(db, changed)
    assert preview["changes"][0]["status"] == "conflict"
    with pytest.raises(Conflict, match="conflicts"):
        import_community_pack(
            db,
            PackConfirmation(pack=changed, confirmation_token=preview["confirmation_token"]),
            actor="test",
        )
    db.refresh(original)
    assert (original.id, original.current_version) == (original_id, original_version)


def test_same_pack_version_with_different_contents_cannot_create_new_definitions(db):
    first = catalog()[0]
    confirm(db, first)
    changed = deepcopy(first)
    changed["trackers"][0]["key"] = "another_energy"
    changed["analysis"] = []
    preview = preview_community_pack(db, changed)
    assert preview["changes"] == [
        {"definition_key": "user.another_energy", "status": "version_conflict"}
    ]
    with pytest.raises(Conflict, match="Pack version"):
        import_community_pack(
            db,
            PackConfirmation(pack=changed, confirmation_token=preview["confirmation_token"]),
            actor="test",
        )
    assert (
        db.scalar(select(EventDefinition).where(EventDefinition.key == "user.another_energy"))
        is None
    )


def test_retired_pack_tracker_is_a_conflict_not_an_installed_form(db):
    pack = catalog()[0]
    confirm(db, pack)
    definition = db.scalar(
        select(EventDefinition).where(EventDefinition.key == "user.daily_energy")
    )
    definition.status = "retired"
    db.flush()
    preview = preview_community_pack(db, pack)
    assert preview["changes"] == [{"definition_key": "user.daily_energy", "status": "conflict"}]
    with pytest.raises(Conflict, match="conflicts"):
        import_community_pack(
            db,
            PackConfirmation(pack=pack, confirmation_token=preview["confirmation_token"]),
            actor="test",
        )


@pytest.mark.parametrize("invalid", ["type", "blank_label", "long_id"])
def test_pack_preview_rejects_tracker_definition_contract_errors(db, invalid):
    pack = deepcopy(catalog()[0])
    draft = pack["trackers"][0]
    if invalid == "type":
        draft["fields"][0]["key"] = "type"
    elif invalid == "blank_label":
        draft["fields"][0]["label"] = "   "
    else:
        draft["key"] = "a" * 63
        draft["fields"][0]["key"] = "b" * 63
        pack["analysis"] = []
    with pytest.raises(ValidationError):
        preview_community_pack(db, pack)


def test_pack_rejects_unbounded_permissions_and_changed_preview(db):
    base = catalog()[2]
    for changed in (
        {"reminder_enabled": True, "reminder_time": "09:00"},
        {"privacy": "private"},
    ):
        pack = deepcopy(base)
        pack["trackers"][0].update(changed)
        with pytest.raises(ValidationError):
            CommunityPack.model_validate(pack)

    bad_recipe = deepcopy(base)
    bad_recipe["analysis"][0]["method"] = "sum"
    with pytest.raises(ValidationError, match="permitted metric operation"):
        CommunityPack.model_validate(bad_recipe)

    preview = preview_community_pack(db, base)
    changed = deepcopy(base)
    changed["trackers"][0]["name"] = "Altered after preview"
    with pytest.raises(Conflict, match="preview changed"):
        import_community_pack(
            db,
            PackConfirmation(pack=changed, confirmation_token=preview["confirmation_token"]),
            actor="test",
        )


@pytest.mark.parametrize(
    ("field", "method"),
    [
        ({"key": "flag", "label": "Flag", "kind": "boolean"}, "rate"),
        (
            {"key": "choice", "label": "Choice", "kind": "choice", "options": ["a", "b"]},
            "mode",
        ),
        (
            {
                "key": "amount",
                "label": "Amount",
                "kind": "number",
                "unit": "count",
                "minimum": 0,
                "maximum": 100,
            },
            "min",
        ),
        (
            {
                "key": "counter",
                "label": "Counter",
                "kind": "number",
                "unit": "count",
                "metric_semantics": "cumulative_counter",
                "minimum": 0,
                "maximum": 100,
            },
            "delta",
        ),
    ],
)
def test_pack_accepts_methods_generated_by_tracker_contracts(db, field, method):
    pack = deepcopy(catalog()[2])
    pack["trackers"][0]["fields"] = [field]
    pack["analysis"] = [
        {
            "label": "Synthetic metric",
            "operation": "aggregate_metric",
            "metric_key": f"user.focus_walk.{field['key']}",
            "method": method,
            "limitation": "Synthetic test only.",
        }
    ]
    assert preview_community_pack(db, pack)["changes"][0]["status"] == "create"


def test_pack_accepts_generated_duration_recipe(db):
    pack = deepcopy(catalog()[2])
    pack["trackers"][0]["topology"] = "bounded_interval"
    pack["trackers"][0]["derived_duration"] = True
    pack["analysis"] = [
        {
            "label": "Elapsed time",
            "operation": "aggregate_metric",
            "metric_key": "user.focus_walk.elapsed_minutes",
            "method": "sum",
            "limitation": "Synthetic test only.",
        }
    ]
    assert preview_community_pack(db, pack)["changes"][0]["status"] == "create"


def test_required_garmin_pack_reports_collection_independently(db):
    configs = ensure_scenario_packs(db, legacy_install=False)
    configs["training"].tracking_enabled = True
    configs["training"].collection_enabled = False
    db.flush()
    required = preview_community_pack(db, catalog()[1])["required_packs"]
    assert required == [{"key": "training", "tracking_enabled": True, "collection_enabled": False}]


def test_system_metric_recipe_requires_matching_system_pack():
    pack = deepcopy(catalog()[0])
    pack["required_packs"] = []
    with pytest.raises(ValidationError, match="requires the sleep system pack"):
        CommunityPack.model_validate(pack)


def test_observation_recipes_cannot_advertise_an_aggregation():
    pack = deepcopy(catalog()[0])
    pack["analysis"][0]["operation"] = "query_observations"
    with pytest.raises(ValidationError, match="return raw rows"):
        CommunityPack.model_validate(pack)
    pack["analysis"][0].pop("method")
    assert CommunityPack.model_validate(pack).analysis[0].method is None


def test_pack_size_limit_counts_utf8_bytes():
    pack = deepcopy(catalog()[2])
    template = pack["trackers"][0]
    template["fields"] = [
        {"key": f"field_{number}", "label": "😀" * 120, "kind": "text"} for number in range(32)
    ]
    pack["trackers"] = [{**deepcopy(template), "key": f"tracker_{number}"} for number in range(8)]
    pack["analysis"] = []
    with pytest.raises(ValidationError, match="size limit"):
        CommunityPack.model_validate(pack)


def test_api_catalog_preview_and_import_require_definition_management(db, db_engine):
    db.commit()
    read_key = "community-read-" + "r" * 32
    manage_key = "community-manage-" + "m" * 32
    client = TestClient(
        create_app(
            Settings(
                api_tokens=[
                    ApiToken(key=read_key, scopes={"read:diary"}),
                    ApiToken(key=manage_key, scopes={"manage:definitions"}),
                ]
            ),
            db_engine,
        )
    )
    pack = catalog()[2]
    read_headers = {"Authorization": "Bearer " + read_key}
    manage_headers = {"Authorization": "Bearer " + manage_key}
    assert client.get("/community-packs", headers=read_headers).status_code == 403
    listed = client.get("/community-packs", headers=manage_headers)
    assert listed.status_code == 200
    assert len(listed.json()["packs"]) == 3
    preview = client.post("/community-packs/preview", json=pack, headers=manage_headers)
    assert preview.status_code == 200
    assert preview.json()["required_packs"] == []
    imported = client.post(
        "/community-packs/import",
        json={"pack": pack, "confirmation_token": preview.json()["confirmation_token"]},
        headers=manage_headers,
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"][0]["action"]["definition_key"] == "user.focus_walk"


def test_api_preview_hides_dependency_state_without_diary_scope(db, db_engine):
    db.commit()
    manage_key = "community-manage-" + "m" * 32
    read_manage_key = "community-both-" + "b" * 32
    client = TestClient(
        create_app(
            Settings(
                api_tokens=[
                    ApiToken(key=manage_key, scopes={"manage:definitions"}),
                    ApiToken(key=read_manage_key, scopes={"manage:definitions", "read:diary"}),
                ]
            ),
            db_engine,
        )
    )
    pack = catalog()[0]
    limited = client.post(
        "/community-packs/preview",
        json=pack,
        headers={"Authorization": "Bearer " + manage_key},
    )
    assert limited.status_code == 200
    assert limited.json()["required_packs"] == [
        {"key": "sleep", "tracking_enabled": None, "collection_enabled": None}
    ]
    full = client.post(
        "/community-packs/preview",
        json=pack,
        headers={"Authorization": "Bearer " + read_manage_key},
    )
    assert full.status_code == 200
    assert isinstance(full.json()["required_packs"][0]["tracking_enabled"], bool)


def test_api_rejects_oversize_raw_pack_json(db, db_engine):
    db.commit()
    manage_key = "community-manage-" + "m" * 32
    client = TestClient(
        create_app(
            Settings(api_tokens=[ApiToken(key=manage_key, scopes={"manage:definitions"})]),
            db_engine,
        )
    )
    headers = {"Authorization": "Bearer " + manage_key, "Content-Type": "application/json"}
    compact = json.dumps(catalog()[2], separators=(",", ":")).encode()
    padded = compact[:-1] + b" " * 65_000 + b"}"
    assert len(compact) < 64_000 < len(padded)
    assert (
        client.post("/community-packs/preview", content=padded, headers=headers).status_code == 413
    )
    streamed = client.post(
        "/community-packs/preview",
        content=iter([b"{" + b"x" * 32_000, b"y" * 33_000]),
        headers=headers,
    )
    assert streamed.status_code == 413
    preview = client.post("/community-packs/preview", content=compact, headers=headers)
    assert preview.status_code == 200, preview.text
    confirmation = json.dumps(
        {"pack": catalog()[2], "confirmation_token": preview.json()["confirmation_token"]},
        separators=(",", ":"),
    ).encode()
    assert (
        client.post(
            "/community-packs/import", content=confirmation + b" " * 65_000, headers=headers
        ).status_code
        == 413
    )
