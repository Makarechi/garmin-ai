from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from garmin_ai.access import permits_tool
from garmin_ai.coffee_sleep import analyze
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import HealthDay, TimelineInterval

END = datetime(2026, 9, 10, 7, tzinfo=UTC)


def night(db, end=END, score=80):
    start = end - timedelta(hours=8)
    db.add(HealthDay(day=end.date(), sleep_score=score, sleep_seconds=28000, sources={}))
    db.add(
        TimelineInterval(
            id=f"sleep:{end.date()}",
            start=start,
            end=end,
            label="sleep",
            source="garmin",
            confidence=1,
            evidence={"source_ref": "synthetic"},
        )
    )
    db.flush()
    return start


def coverage(db, start, end, kind="caffeine_log_complete", status="confirmed"):
    return create_event(
        db,
        EventInput(
            start=start,
            end=end,
            timezone="UTC",
            status=status,
            payload={"type": kind, "description": "synthetic explicit coverage"},
        ),
        actor="owner",
    )


def coffee(db, at):
    return create_event(
        db,
        EventInput(
            start=at,
            timezone="UTC",
            payload={
                "type": "caffeine",
                "beverage": "synthetic",
                "dose_basis": "total",
                "caffeine_mg_min": 50,
                "caffeine_mg_max": 90,
            },
        ),
        actor="owner",
    )


def test_missing_diary_is_unknown_and_not_zero(db):
    night(db)
    result = analyze(db, END.date(), END.date())
    row = result["rows"][0]
    assert not row["eligible"] and "incomplete_caffeine_diary" in row["exclusions"]
    assert row["total_caffeine_mg"] == {"min": None, "estimate": None, "max": None}
    assert result["comparison"] is None and result["status"] == "insufficient_evidence"


def test_explicit_complete_window_supports_zero_and_preserves_dose_range(db):
    bedtime = night(db)
    coverage(db, bedtime - timedelta(hours=24), bedtime)
    assert analyze(db, END.date(), END.date())["rows"][0]["total_caffeine_mg"]["estimate"] == 0
    coffee(db, bedtime - timedelta(hours=2))
    result = analyze(db, END.date(), END.date())
    row = result["rows"][0]
    assert row["eligible"] and row["cohort"] == "late"
    assert row["total_caffeine_mg"] == {"min": 50, "estimate": None, "max": 90}
    assert row["last_recorded_caffeine_hours_before_sleep"] == 2
    assert result["status"] == "insufficient_evidence"


@pytest.mark.parametrize("condition", ["gap", "unconfirmed", "conflict", "illness"])
def test_ineligible_windows_are_explicitly_excluded(db, condition):
    bedtime = night(db)
    left = bedtime - timedelta(hours=24)
    if condition == "gap":
        coverage(db, left, bedtime - timedelta(seconds=1))
    else:
        coverage(
            db,
            left,
            bedtime,
            kind="caffeine_absence" if condition == "conflict" else "caffeine_log_complete",
            status="needs_confirmation" if condition == "unconfirmed" else "confirmed",
        )
    if condition == "conflict":
        coffee(db, bedtime - timedelta(hours=1))
    if condition == "illness":
        coverage(db, left - timedelta(days=1), bedtime, kind="illness")
    result = analyze(db, END.date(), END.date())
    assert result["excluded"] == 1 and result["eligible"] == 0


def test_adjacent_coverage_is_unioned_and_bedtime_boundary_is_exclusive(db):
    bedtime = night(db)
    coverage(db, bedtime - timedelta(hours=24), bedtime - timedelta(hours=12))
    coverage(db, bedtime - timedelta(hours=12), bedtime)
    coffee(db, bedtime)
    row = analyze(db, END.date(), END.date())["rows"][0]
    assert row["eligible"] and row["cohort"] == "not_late"


def test_exploratory_cohorts_and_hash_are_reproducible(db):
    for offset in range(10):
        bedtime = night(db, END + timedelta(days=offset), score=70 if offset % 2 else 80)
        coverage(db, bedtime - timedelta(hours=24), bedtime)
        if offset % 2:
            coffee(db, bedtime - timedelta(hours=2))
    result = analyze(db, END.date(), (END + timedelta(days=9)).date())
    assert result["eligible"] == 10 and result["status"] == "exploratory"
    assert result["comparison"]["difference"] == -10
    assert result["comparison"]["ci95"] is None
    assert (
        result["evidence_hash"]
        == analyze(db, END.date(), (END + timedelta(days=9)).date())["evidence_hash"]
    )


def test_coverage_requires_interval_and_tool_needs_both_scopes():
    with pytest.raises(ValidationError):
        EventInput(start=END, payload={"type": "caffeine_log_complete", "description": "synthetic"})
    assert permits_tool({"read:diary", "read:health"}, "analysis_coffee_sleep")
    assert not permits_tool({"read:health"}, "analysis_coffee_sleep")
    assert not permits_tool({"read:diary"}, "analysis_coffee_sleep")
