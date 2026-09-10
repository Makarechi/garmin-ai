from datetime import UTC, datetime, timedelta

import pytest

from garmin_ai.events import EventInput, create_event
from garmin_ai.queries import list_events
from garmin_ai.telegram import diary_label

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "basis,low,high,expected",
    [("total", 100, 160, (100, 160)), ("per_serving", 50, 80, (100, 160))],
)
def test_two_espressos_have_one_total_dose(db, basis, low, high, expected):
    event = create_event(
        db,
        EventInput(
            start=NOW,
            timezone="UTC",
            payload={
                "type": "caffeine",
                "beverage": "synthetic espresso",
                "servings": 2,
                "dose_basis": basis,
                "dose_provenance": "estimated",
                "caffeine_mg_min": low,
                "caffeine_mg_max": high,
            },
        ),
        actor="test",
    )
    result = list_events(db, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    total = result["rows"][0]["caffeine_total"]
    assert (total["min"], total["max"]) == expected
    assert total["source_basis"] == basis and total["provenance"] == "estimated"
    label = diary_label(event)
    assert "100–160 мг" in label and "оценка" in label
    assert event.payload["caffeine_mg_min"] == low


def test_legacy_ambiguous_dose_remains_unknown_without_rewriting_payload(db):
    row = create_event(
        db,
        EventInput(
            start=NOW,
            payload={
                "type": "caffeine",
                "beverage": "synthetic",
                "servings": 2,
                "caffeine_mg_estimate": 120,
            },
        ),
        actor="test",
    )
    row.payload = {
        key: value
        for key, value in row.payload.items()
        if key not in {"dose_basis", "dose_provenance", "dose_notes"}
    }
    db.flush()
    result = list_events(db, NOW - timedelta(hours=1), NOW + timedelta(hours=1))["rows"][0]
    assert result["caffeine_total"]["status"] == "unknown"
    assert result["caffeine_total"]["estimate"] is None
    assert result["payload"]["caffeine_mg_estimate"] == 120
    assert "неизвестна" in diary_label(row)


def test_reported_label_is_distinct_from_estimate(db):
    row = create_event(
        db,
        EventInput(
            start=NOW,
            payload={
                "type": "caffeine",
                "beverage": "synthetic",
                "dose_basis": "total",
                "dose_provenance": "reported_label",
                "dose_notes": "user-reported label",
                "caffeine_mg_estimate": 80,
            },
        ),
        actor="test",
    )
    assert "80 мг (по этикетке)" in diary_label(row)
    assert "около" not in diary_label(row)
