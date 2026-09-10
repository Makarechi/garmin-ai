import json
from datetime import UTC, date, datetime, timedelta

import pytest

from garmin_ai.access import permits_tool
from garmin_ai.archive import LocalArchive
from garmin_ai.events import EventInput, create_event
from garmin_ai.ingest import ingest
from garmin_ai.llm import compact
from garmin_ai.sleep_analysis import regularity_minutes
from garmin_ai.tools import call_tool

DAY = date(2026, 9, 10)
END = datetime(2026, 9, 10, 7, tzinfo=UTC)


def night(db, tmp_path, day=DAY):
    end = datetime.combine(day, END.time(), UTC)
    return ingest(
        db,
        LocalArchive(tmp_path),
        "sleep",
        str(day),
        {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": int((end - timedelta(hours=8)).timestamp() * 1000),
                "sleepEndTimestampGMT": int(end.timestamp() * 1000),
                "sleepTimeSeconds": 27000,
                "deepSleepSeconds": 6000,
                "remSleepSeconds": 6000,
                "lightSleepSeconds": 15000,
                "awakeSleepSeconds": 1800,
                "sleepScores": {"overall": {"value": 80}},
            }
        },
        "UTC",
    )


def analyze(db, **kwargs):
    db.info["timezone"] = "UTC"
    return call_tool(db, "analysis_sleep", {"start": str(DAY), "end": str(DAY), **kwargs})


def test_raw_sleep_stages_reach_tool_and_calculation_with_units_and_provenance(db, tmp_path):
    raw = night(db, tmp_path)
    result = analyze(db)
    row = result["rows"][0]
    assert row["unavailable"] == []
    assert set(row["source_refs"].values()) == {raw["source_ref"]}
    assert result["units"]["deep_seconds"] == "s"
    assert result["units"]["sleep_score"] == "score"
    assert row["stage_fractions"]["deep_seconds"] == pytest.approx(6000 / 27000)
    assert result["summary"]["mean_documented_sleep_seconds"] == 27000
    assert row["nap_status"] == "unanswered" and row["reported_nap_seconds"] is None


@pytest.mark.parametrize("nap_policy,expected", [("separate", 27000), ("include_confirmed", 28800)])
def test_main_sleep_and_nap_on_same_date_do_not_overwrite_each_other(
    db, tmp_path, nap_policy, expected
):
    night(db, tmp_path)
    nap = create_event(
        db,
        EventInput(
            start=END + timedelta(hours=6),
            end=END + timedelta(hours=6, minutes=30),
            payload={"type": "nap", "description": "synthetic nap"},
        ),
        actor="test",
    )
    result = analyze(db, nap_policy=nap_policy)
    row = result["rows"][0]
    assert row["main_interval"]["end"] == END.isoformat()
    assert row["naps"][0]["event_id"] == str(nap.id)
    assert row["values"]["sleep_seconds"] == 27000
    assert row["documented_sleep_seconds"] == expected


def test_missing_sleep_and_untimed_nap_are_unknown(db):
    create_event(
        db, EventInput(start=END, payload={"type": "nap", "description": "synthetic"}), actor="test"
    )
    result = analyze(db, nap_policy="include_confirmed")
    assert result["summary"]["mean_documented_sleep_seconds"] is None
    assert result["rows"][0]["nap_status"] == "incomplete_interval"
    assert result["rows"][0]["documented_sleep_seconds"] is None


def test_nap_overlapping_main_sleep_is_not_double_counted(db, tmp_path):
    night(db, tmp_path)
    create_event(
        db,
        EventInput(
            start=END - timedelta(minutes=20),
            end=END + timedelta(minutes=10),
            payload={"type": "nap", "description": "synthetic"},
        ),
        actor="test",
    )
    row = analyze(db, nap_policy="include_confirmed")["rows"][0]
    assert row["nap_status"] == "overlaps_main"
    assert row["documented_sleep_seconds"] is None


def test_sleep_regularity_handles_midnight_without_24_hour_jump():
    anchor = END.replace(hour=0)
    assert (
        regularity_minutes([anchor - timedelta(minutes=1), anchor, anchor + timedelta(minutes=1)])
        < 1
    )
    assert regularity_minutes([anchor]) is None


def test_month_summary_fits_model_budget_and_requires_both_scopes(db, tmp_path):
    for offset in range(31):
        night(db, tmp_path, DAY + timedelta(days=offset))
    result = analyze(db, end=str(DAY + timedelta(days=30)))
    assert json.loads(compact(result))["summary"]["available_sleep_days"] == 31
    assert permits_tool({"read:health", "read:diary"}, "analysis_sleep")
    assert not permits_tool({"read:health"}, "analysis_sleep")
