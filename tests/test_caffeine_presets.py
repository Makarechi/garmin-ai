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


def test_webhook_preset_keeps_snapshot_until_explicit_time_without_model(db, db_engine):
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from garmin_ai.api import create_app
    from garmin_ai.telegram import process_message, save_update

    recipe = preset()
    config = Settings(
        caffeine_presets=[recipe],
        telegram_user_id=42,
        timezone="UTC",
        telegram_webhook_secret="synthetic-webhook-secret",
    )
    now = datetime.now(UTC) - timedelta(minutes=3)
    client = TestClient(create_app(config, db_engine))
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic-webhook-secret"},
        json={
            "update_id": 301,
            "callback_query": {
                "id": "synthetic",
                "from": {"id": 42},
                "data": callback(recipe),
                "message": {
                    "message_id": 300,
                    "date": now.isoformat(),
                    "chat": {"id": 42, "type": "private"},
                },
            },
        },
    )
    assert response.status_code == 200
    assert "Укажите время" in process_message(db_engine, None, config, 301)
    assert db.scalar(select(Event)) is None
    recipe.recipe.caffeine_mg_max = 500
    for identity, message in [(302, "непонятное время"), (303, "сейчас")]:
        save_update(
            db,
            {
                "update_id": identity,
                "message": {
                    "message_id": identity,
                    "date": (now + timedelta(minutes=2)).isoformat(),
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": message,
                },
            },
            42,
        )
        db.commit()
        result = process_message(db_engine, None, config, identity)
        if identity == 302:
            assert "Не удалось" in result
            db.expire_all()
            assert (
                db.get(AppState, "conversation:pending").value["preset_recipe"]["caffeine_mg_max"]
                == 60
            )
    assert process_message(db_engine, None, config, 303) == result
    db.expire_all()
    events = db.scalars(select(Event)).all()
    assert len(events) == 1 and events[0].payload["caffeine_mg_max"] == 60
    assert events[0].start == now + timedelta(minutes=2)


def test_rendered_preset_labels_cannot_collide_after_truncation():
    first = preset()
    second = preset(beverage="different synthetic recipe")
    first.name = "A" * 50 + " first"
    second.name = "A" * 50 + " second"
    with pytest.raises(ValidationError, match="button labels must be distinct"):
        Settings(caffeine_presets=[first, second])
    second.name = "B" + second.name[1:]
    assert len(Settings(caffeine_presets=[first, second]).caffeine_presets) == 2


@pytest.mark.parametrize("selected", ["preset", "unspecified"])
@pytest.mark.parametrize("voice", [False, True])
def test_unknown_click_time_uses_immutable_deterministic_form(db, db_engine, selected, voice):
    from garmin_ai.agent import SafetyScreen
    from garmin_ai.telegram import process_message, save_update

    recipe = preset()
    config = Settings(telegram_user_id=42, timezone="UTC", caffeine_presets=[recipe])
    now = datetime.now(UTC)
    token = callback(recipe) if selected == "preset" else "coffee:unspecified"
    handle_button(db, token, config, "owner", 800, now, time_known=False)
    recipe.recipe.caffeine_mg_max = 500
    message = {
        "message_id": 801,
        "date": now.isoformat(),
        "from": {"id": 42},
        "chat": {"id": 42, "type": "private"},
        **({"voice": {"file_id": "synthetic"}} if voice else {"text": "сейчас"}),
    }
    save_update(db, {"update_id": 801, "message": message}, 42)
    db.commit()

    class Provider:
        def structured(self, instruction, prompt, schema):
            assert schema is SafetyScreen, (
                "The selected recipe must never be regenerated by a model"
            )
            return SafetyScreen(urgent=False)

    provider = Provider() if voice else None
    result = process_message(db_engine, provider, config, 801, "сейчас" if voice else None)
    assert "Сохранил" in result
    assert process_message(db_engine, provider, config, 801, "сейчас" if voice else None) == result
    rows = db.scalars(select(Event)).all()
    assert len(rows) == 1 and rows[0].start == now
    assert rows[0].source == ("telegram_voice" if voice else "telegram_text")
    if selected == "preset":
        assert rows[0].payload["caffeine_mg_max"] == 60
    else:
        assert caffeine_total(rows[0].payload)["status"] == "unknown"


def test_invalid_preset_reply_cannot_extend_selection_lifetime(db):
    from datetime import timedelta

    from garmin_ai.agent import Interpretation, apply_command, pending_clarification

    recipe = preset()
    config = Settings(timezone="UTC", caffeine_presets=[recipe])
    handle_button(db, callback(recipe), config, "owner", 900, NOW, time_known=False)
    db.info["conversation_now"] = NOW + timedelta(minutes=90)
    apply_command(
        db,
        Interpretation(intent="clarify", confidence=0),
        text="bad time",
        update_id=901,
        actor="owner",
        now=NOW + timedelta(minutes=90),
    )
    assert pending_clarification(db, NOW + timedelta(minutes=90)) is not None
    db.info["conversation_now"] = NOW + timedelta(hours=2, seconds=1)
    assert pending_clarification(db, NOW + timedelta(hours=2, seconds=1)) is None


@pytest.mark.parametrize("button", ["medication", "note"])
def test_natural_voice_forms_keep_model_interpretation(db, db_engine, button):
    from garmin_ai.agent import Interpretation
    from garmin_ai.telegram import process_message, save_update

    now = datetime.now(UTC)
    config = Settings(telegram_user_id=42, timezone="UTC")
    handle_button(db, button, config, "owner", 950, now, time_known=False)
    save_update(
        db,
        {
            "update_id": 951,
            "message": {
                "message_id": 951,
                "date": now.isoformat(),
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "voice": {"file_id": "synthetic"},
            },
        },
        42,
    )
    db.commit()

    class Provider:
        def structured(self, instruction, prompt, schema):
            assert schema is Interpretation
            return Interpretation(
                intent="clarify", confidence=0, clarification="synthetic model clarification"
            )

    assert (
        process_message(db_engine, Provider(), config, 951, "синтетическая голосовая запись сейчас")
        == "synthetic model clarification"
    )
