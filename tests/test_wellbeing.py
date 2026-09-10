from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from garmin_ai.access import permits_tool
from garmin_ai.events import EventInput, create_event, delete_event, update_event
from garmin_ai.models import HealthDay
from garmin_ai.telegram import diary_label
from garmin_ai.tools import call_tool

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def report(**payload):
    return EventInput(
        start=NOW, timezone="UTC", payload={"type": "wellbeing_observation", **payload}
    )


@pytest.mark.parametrize(
    "payload", [{}, {"energy": 11}, {"energy": True}, {"pain": -1}, {"notes": "   "}]
)
def test_invalid_or_empty_reports_are_rejected(payload):
    with pytest.raises(ValidationError):
        report(**payload)


def test_reports_remain_independent_of_high_vendor_score(db):
    db.add(HealthDay(day=NOW.date(), body_battery_high=99))
    row = create_event(db, report(notes="Чувствую себя разбитым", energy=1), actor="owner")
    result = call_tool(
        db, "wellbeing_observations", {"start": NOW, "end": NOW + timedelta(hours=1)}
    )
    assert result["rows"][0]["payload"]["notes"] == "Чувствую себя разбитым"
    assert result["rows"][0]["payload"]["energy"] == 1
    assert result["rows"][0]["payload"]["restedness"] is None
    assert result["rows"][0]["topology"] == "point"
    assert result["evidence_type"] == "subjective_diary"
    assert "разбитым" in diary_label(row)
    assert db.get(HealthDay, NOW.date()).body_battery_high == 99


def test_reports_support_existing_idempotency_corrections_and_deletion(db):
    row = create_event(db, report(energy=0), actor="owner", idempotency_key="synthetic")
    assert (
        create_event(db, report(energy=0), actor="owner", idempotency_key="synthetic").id == row.id
    )
    update_event(db, row.id, report(energy=2), revision=row.revision, actor="owner")
    assert row.payload["energy"] == 2
    delete_event(db, row.id, revision=row.revision, actor="owner")
    result = call_tool(
        db, "wellbeing_observations", {"start": NOW, "end": NOW + timedelta(hours=1)}
    )
    assert result["rows"] == [] and result["missingness"] == "unreported_is_unknown"


def test_subjective_read_needs_diary_permission_only():
    assert permits_tool({"read:diary"}, "wellbeing_observations")
    assert not permits_tool({"read:health"}, "wellbeing_observations")


def test_report_interval_is_point_and_query_is_half_open(db):
    with pytest.raises(ValidationError, match="point-in-time"):
        EventInput(
            start=NOW,
            end=NOW + timedelta(hours=1),
            payload={"type": "wellbeing_observation", "pain": 2},
        )
    create_event(db, report(notes="synthetic"), actor="owner")
    assert (
        call_tool(db, "wellbeing_observations", {"start": NOW - timedelta(hours=1), "end": NOW})[
            "rows"
        ]
        == []
    )
    with pytest.raises(ValueError, match="bounded"):
        call_tool(db, "wellbeing_observations", {"start": NOW, "end": NOW + timedelta(days=32)})


def test_large_valid_report_remains_retrievable_with_explicit_omissions(db):
    item = report(notes="я" * 4000, energy=2)
    item.original_text = "я" * 16000
    create_event(db, item, actor="owner")
    result = call_tool(
        db, "wellbeing_observations", {"start": NOW, "end": NOW + timedelta(hours=1)}
    )
    evidence = result["rows"][0]
    assert "original_text" not in evidence
    assert "original_text" in evidence["omitted_fields"]
    assert evidence["notes_truncated"]
    assert evidence["payload"]["energy"] == 2


def test_correction_can_clear_one_rating_without_changing_another(db):
    from garmin_ai.agent import Interpretation, apply_command

    row = create_event(db, report(energy=2, pain=5), actor="owner")
    command = Interpretation(
        intent="update",
        confidence=1,
        target_event_id=row.id,
        changed_fields=["payload.energy"],
        events=[report(energy=None, pain=5)],
    )
    response = apply_command(
        db, command, text="Убери оценку энергии", update_id=10, actor="owner", now=NOW
    )
    assert row.payload["energy"] is None and row.payload["pain"] == 5
    assert "самочувствие" in response and "wellbeing_observation" not in response


@pytest.mark.parametrize("source,status", [("inferred", "inferred"), ("manual", "inferred")])
def test_inferred_wellbeing_cannot_be_written(source, status):
    with pytest.raises(ValidationError, match="explicit user reports"):
        EventInput(
            start=NOW,
            source=source,
            status=status,
            payload={"type": "wellbeing_observation", "energy": 4},
        )


def test_legacy_inferred_or_unconfirmed_rows_are_not_subjective_evidence(db):
    from garmin_ai.models import Event

    for source, status in [("inferred", "inferred"), ("manual", "needs_confirmation")]:
        db.add(
            Event(
                start=NOW,
                timezone="UTC",
                kind="wellbeing_observation",
                source=source,
                status=status,
                payload={"type": "wellbeing_observation", "energy": 4},
            )
        )
    db.flush()
    assert (
        call_tool(db, "wellbeing_observations", {"start": NOW, "end": NOW + timedelta(hours=1)})[
            "rows"
        ]
        == []
    )


def test_calendar_overflow_is_invalid_arguments(db):
    from datetime import datetime

    from garmin_ai.wellbeing import observations

    with pytest.raises(ValueError, match="calendar"):
        observations(db, datetime.fromisoformat("0001-01-01T00:00:00+01:00"), NOW)


def test_legacy_inferred_delete_can_be_undone_without_becoming_evidence(db):
    from garmin_ai.events import delete_event, undo_last
    from garmin_ai.models import Event

    row = Event(
        start=NOW,
        timezone="UTC",
        kind="wellbeing_observation",
        source="inferred",
        status="inferred",
        payload={"type": "wellbeing_observation", "energy": 4},
    )
    db.add(row)
    db.flush()
    delete_event(db, row.id, revision=row.revision, actor="owner")
    undo_last(db, actor="owner")
    assert not row.deleted and row.status == "inferred"
    assert (
        call_tool(db, "wellbeing_observations", {"start": NOW, "end": NOW + timedelta(hours=1)})[
            "rows"
        ]
        == []
    )


def test_equal_end_point_is_canonicalized_and_start_only_correction_works(db):
    from garmin_ai.agent import Interpretation, apply_command

    row = create_event(
        db,
        EventInput(
            start=NOW,
            end=NOW,
            timezone="UTC",
            payload={"type": "wellbeing_observation", "energy": 4},
        ),
        actor="owner",
    )
    assert row.end is None
    command = Interpretation(
        intent="update",
        confidence=1,
        target_event_id=row.id,
        changed_fields=["start"],
        events=[
            EventInput(
                start=NOW + timedelta(hours=1),
                timezone="UTC",
                payload={"type": "wellbeing_observation", "energy": 4},
            )
        ],
    )
    apply_command(
        db, command, text="часом позже", update_id=1, actor="owner", now=NOW + timedelta(hours=2)
    )
    assert row.start == NOW + timedelta(hours=1) and row.end is None


def test_same_timestamp_large_reports_paginate_without_loss(db):
    import json

    identities = {
        str(create_event(db, report(notes="я" * 4000, energy=2), actor="owner").id)
        for _ in range(15)
    }
    seen, cursor = [], None
    for _ in range(15):
        page = call_tool(
            db,
            "wellbeing_observations",
            {"start": NOW, "end": NOW + timedelta(hours=1), "cursor": cursor},
        )
        assert page["rows"]
        assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= 20000
        seen.extend(row["id"] for row in page["rows"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and len(seen) == len(set(seen)) == 15
    assert set(seen) == identities
