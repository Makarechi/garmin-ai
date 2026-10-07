"""Community packs create only owner-confirmed, isolated tracker definitions."""

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
    imported = client.post(
        "/community-packs/import",
        json={"pack": pack, "confirmation_token": preview.json()["confirmation_token"]},
        headers=manage_headers,
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"][0]["action"]["definition_key"] == "user.focus_walk"
