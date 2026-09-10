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
    with pytest.raises(ValueError, match="31 days"):
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
