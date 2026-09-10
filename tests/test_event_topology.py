from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from garmin_ai.events import EventInput, create_event
from garmin_ai.queries import list_events, timeline


@pytest.mark.parametrize("status", ["confirmed", "inferred", "needs_confirmation"])
@pytest.mark.parametrize("kind", ["illness", "migraine"])
def test_old_open_illness_remains_targetable_and_closable(db, status, kind):
    from garmin_ai.agent import Interpretation, apply_command, context_for, interpret
    from garmin_ai.config import Settings

    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    original = EventInput(
        start=now - timedelta(days=30),
        timezone="UTC",
        status=status,
        payload={
            "type": kind,
            **({"description": "synthetic illness"} if kind == "illness" else {}),
        },
    )
    illness = create_event(db, original, actor="test")
    other = "migraine" if kind == "illness" else "illness"
    for days in range(40, 61):
        create_event(
            db,
            EventInput(
                start=now - timedelta(days=days),
                payload={
                    "type": other,
                    **({"description": "synthetic"} if other == "illness" else {}),
                },
            ),
            actor="test",
        )
    for minutes in range(13):
        create_event(
            db,
            EventInput(
                start=now - timedelta(minutes=minutes),
                payload={"type": "note", "description": "synthetic"},
            ),
            actor="test",
        )
    assert str(illness.id) in {row["id"] for row in context_for(db, now)["recent_events"]}
    proposed = EventInput(
        start=now - timedelta(hours=1),
        end=now,
        timezone="UTC",
        payload={
            "type": kind,
            **({"description": "must not overwrite"} if kind == "illness" else {}),
        },
    )

    class Provider:
        def structured(self, *args):
            return Interpretation(
                intent="close", events=[proposed], target_event_id=illness.id, confidence=1
            )

    text = "болезнь закончилась" if kind == "illness" else "мигрень закончилась"
    command = interpret(db, Provider(), text, Settings(), now)
    if status != "confirmed":
        assert command.intent == "clarify"
        return
    assert command.intent == "close"
    apply_command(
        db,
        command,
        text="болезнь закончилась",
        update_id=100,
        actor="test",
        now=now,
    )
    db.refresh(illness)
    assert illness.start == original.start and illness.end == now
    if kind == "illness":
        assert illness.payload["description"] == "synthetic illness"


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
