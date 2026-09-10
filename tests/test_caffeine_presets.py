from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from garmin_ai.caffeine_presets import CaffeinePreset, callback, keyboard
from garmin_ai.config import Settings
from garmin_ai.events import caffeine_total
from garmin_ai.models import AppState, Event
from garmin_ai.telegram import handle_button

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def preset(**changes):
    return CaffeinePreset(
        id=uuid4(),
        name="Synthetic drink",
        recipe={
            "type": "caffeine",
            "beverage": "Synthetic recipe",
            "servings": 2,
            "dose_basis": "per_serving",
            "dose_provenance": "estimated",
            "caffeine_mg_min": 40,
            "caffeine_mg_max": 60,
            **changes,
        },
    )


def test_selection_preserves_owner_recipe_and_is_idempotent(db):
    recipe = preset()
    settings = Settings(caffeine_presets=[recipe])
    response = handle_button(db, "coffee", settings, "owner", 1, NOW)
    assert "Выберите" in response and db.scalar(select(Event)) is None
    token = keyboard(settings.caffeine_presets)["inline_keyboard"][0][0]["callback_data"]
    assert len(token.encode()) <= 64
    for _ in range(2):
        handle_button(db, token, settings, "owner", 2, NOW)
    rows = db.scalars(select(Event)).all()
    assert len(rows) == 1 and rows[0].payload == recipe.recipe.model_dump(mode="json")
    assert caffeine_total(rows[0].payload)["min"] == 80
    assert db.get(AppState, "conversation:pending").value["optional_refinement"]


def test_changed_recipe_rejects_old_callback_without_mutating_old_events(db):
    recipe = preset()
    settings = Settings(caffeine_presets=[recipe])
    old_token = callback(recipe)
    handle_button(db, old_token, settings, "owner", 1, NOW)
    recipe.recipe.caffeine_mg_max = 80
    response = handle_button(db, old_token, settings, "owner", 2, NOW)
    assert "изменён" in response
    rows = db.scalars(select(Event)).all()
    assert len(rows) == 1 and rows[0].payload["caffeine_mg_max"] == 60


def test_unknown_drink_and_missing_click_time_do_not_invent_dose(db):
    settings = Settings(caffeine_presets=[preset()])
    handle_button(
        db, callback(settings.caffeine_presets[0]), settings, "owner", 1, NOW, time_known=False
    )
    assert db.scalar(select(Event)) is None
    handle_button(db, "coffee:unspecified", settings, "owner", 2, NOW)
    assert caffeine_total(db.scalar(select(Event)).payload)["status"] == "unknown"


def test_presets_validate_duplicate_identity_and_invalid_ranges():
    recipe = preset()
    with pytest.raises(ValidationError, match="distinct"):
        Settings(caffeine_presets=[recipe, recipe])
    with pytest.raises(ValidationError):
        preset(caffeine_mg_min=100)
    assert Settings().caffeine_presets == []
