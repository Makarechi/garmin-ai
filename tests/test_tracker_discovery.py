"""Synthetic checks for choosing trackers when lexical ranking is inconclusive."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.natural_language import process_tracker_text
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)


def install_tracker(db, key, label, *, privacy="private"):
    draft = TrackerSetupDraft(
        key=key,
        name=label,
        locale="en",
        privacy=privacy,
        fields=[
            TrackerFieldDraft(key="rating", label="Rating", kind="scale", minimum=1, maximum=5)
        ],
    )
    preview = preview_tracker(db, draft)
    return confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )


class NoProviderCalls:
    def structured(self, *_args):
        pytest.fail("An unranked selection must not send metadata or text to the model")


@pytest.mark.parametrize("channel", [None, "telegram:primary"])
@pytest.mark.parametrize("text", ["Sústredenie pri práci", "Sústredenie pri Waterfall"])
def test_unranked_slovak_request_offers_all_allowed_trackers(db, channel, text):
    for key, label in [
        ("a_breathing", "Breathing"),
        ("b_food", "Food"),
        ("c_water", "Water"),
        ("d_weight", "Weight"),
        ("e_walk", "Walk"),
        ("z_focus", "Focus"),
    ]:
        install_tracker(db, key, label)
    install_tracker(db, "f_hidden", "Synthetic private condition", privacy="sensitive")
    if channel:
        db.info["channel_destination_instance_id"] = channel

    result = process_tracker_text(
        db,
        NoProviderCalls(),
        {"text": text, "operation_id": "synthetic-discovery"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=datetime(2026, 10, 22, 14, tzinfo=UTC),
    )

    assert result["intent"] == "deterministic_form"
    assert result["reason"] == "tracker_selection_required"
    assert result["written"] is False
    assert result["selection_has_more"] is False
    keys = {form["action"]["definition_key"] for form in result["forms"]}
    assert "user.z_focus" in keys
    assert ("user.f_hidden" in keys) is (channel is None)
    if channel:
        assert "Synthetic private condition" not in str(result)


def test_authenticated_http_offers_the_tracker_beyond_the_first_five(db, db_engine):
    for index in range(6):
        install_tracker(db, f"tracker_{index}", f"Synthetic label {index}")
    db.commit()
    key = "synthetic-discovery-http-" + "x" * 32
    client = TestClient(
        create_app(
            Settings(_env_file=None, api_tokens=[ApiToken(key=key, scopes={"read:diary"})]),
            db_engine,
        )
    )
    response = client.post(
        "/natural-language/trackers",
        headers={"Authorization": "Bearer " + key},
        json={"text": "Sústredenie pri práci", "operation_id": "synthetic-http-discovery"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["reason"] == "tracker_selection_required"
    assert len(result["forms"]) == 6
    assert not result["written"]


def test_selection_is_bounded_and_reports_additional_choices(db):
    for index in range(51):
        install_tracker(db, f"item_{index:02d}", f"Synthetic label {index}")
    result = process_tracker_text(
        db,
        NoProviderCalls(),
        {"text": "Sústredenie pri práci", "operation_id": "synthetic-large-discovery"},
        granted={"read:diary"},
        actor="test",
        now=datetime(2026, 10, 22, 14, tzinfo=UTC),
    )
    assert len(result["forms"]) == 50
    assert result["selection_has_more"] is True
