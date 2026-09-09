from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from garmin_ai.events import EventInput, create_event
from garmin_ai.queries import list_events, timeline


@pytest.mark.parametrize("day", ["2026-03-29", "2026-10-25"])
def test_open_episodes_survive_midnight_and_dst(db, day):
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Europe/Budapest"))
    end = start + timedelta(days=1)
    episodes = []
    for payload in [
        {"type": "migraine"},
        {"type": "illness", "description": "synthetic illness"},
        {"type": "caffeine", "beverage": "synthetic coffee"},
        {"type": "note", "description": "synthetic note"},
    ]:
        row = create_event(
            db, EventInput(start=start - timedelta(hours=6), payload=payload), actor="test"
        )
        if payload["type"] in {"migraine", "illness"}:
            episodes.append(row)
    result = list_events(db, start, end)
    assert {r["id"] for r in result["rows"]} == {str(r.id) for r in episodes}
    assert timeline(db, start, end)["events"] == result
    for row in result["rows"]:
        assert row["topology"] == "open_interval"
        assert row["ongoing"] is True
        assert row["missing_end"] is True
        assert row["end"] is None
        assert datetime.fromisoformat(row["start"]) < start
    for episode in episodes:
        db.refresh(episode)
        assert episode.end is None


@pytest.mark.parametrize(
    "left,right,deleted,status,included,topology",
    [
        (-2, 0, False, "confirmed", False, "bounded_interval"),
        (24, None, False, "confirmed", False, "open_interval"),
        (-2, None, True, "confirmed", False, "open_interval"),
        (-2, None, False, "inferred", True, "open_interval"),
        (-2, None, False, "needs_confirmation", True, "open_interval"),
        (0, 0, False, "confirmed", True, "point"),
        (-2, -2, False, "confirmed", False, "point"),
        (-2, 2, False, "confirmed", True, "bounded_interval"),
    ],
)
def test_event_half_open_boundaries_and_status(
    db, left, right, deleted, status, included, topology
):
    start = datetime(2026, 9, 10, tzinfo=UTC)
    row = create_event(
        db,
        EventInput(
            start=start + timedelta(hours=left),
            end=None if right is None else start + timedelta(hours=right),
            status=status,
            payload={"type": "migraine"},
        ),
        actor="test",
    )
    row.deleted = deleted
    db.flush()
    result = list_events(db, start, start + timedelta(days=1), kind="migraine")
    assert bool(result["rows"]) is included
    if included:
        assert result["rows"][0]["topology"] == topology
        assert result["rows"][0]["status"] == status


def test_point_event_is_not_ongoing_and_pagination_is_stable(db):
    start = datetime(2026, 9, 10, tzinfo=UTC)
    for _ in range(3):
        create_event(
            db,
            EventInput(start=start, payload={"type": "caffeine", "beverage": "test"}),
            actor="test",
        )
    result = list_events(db, start, start + timedelta(days=1), limit=2)
    assert result["truncated"] is True
    assert result == list_events(db, start, start + timedelta(days=1), limit=2)
    for row in result["rows"]:
        assert row["topology"] == "point"
        assert row["ongoing"] is False
        assert row["missing_end"] is False


def test_old_open_episodes_cannot_hide_in_window_events(db):
    start = datetime(2026, 9, 10, tzinfo=UTC)
    for days in (100, 50, 10):
        create_event(
            db,
            EventInput(start=start - timedelta(days=days), payload={"type": "migraine"}),
            actor="test",
        )
    recent = create_event(
        db,
        EventInput(
            start=start + timedelta(hours=1), payload={"type": "caffeine", "beverage": "synthetic"}
        ),
        actor="test",
    )
    result = list_events(db, start, start + timedelta(days=1), limit=2)
    assert result["truncated"] is True
    assert result["rows"][0]["id"] == str(recent.id)
    assert result["rows"][1]["topology"] == "open_interval"
