from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from garmin_ai.access import TOOL_SCOPES
from garmin_ai.models import Measurement
from garmin_ai.stress_analysis import _split_local_hours, stress_by_hour
from garmin_ai.tools import TOOLS, call_tool


class Session:
    def __init__(self, samples):
        self.samples = samples

    def execute(self, _query):
        return self

    def all(self):
        return self.samples


def samples_for_hour(day, hour, score):
    start = datetime(2026, 9, day, hour, tzinfo=UTC)
    return [
        SimpleNamespace(ts=start + timedelta(minutes=5 * index), value=score) for index in range(13)
    ]


def test_stress_hours_rank_measured_time_not_diary_entries():
    samples = sorted(
        [
            sample
            for day in (1, 2, 3)
            for hour, score in ((9, 80), (16, 20))
            for sample in samples_for_hour(day, hour, score)
        ],
        key=lambda sample: sample.ts,
    )
    result = stress_by_hour(Session(samples), date(2026, 9, 1), date(2026, 9, 3), "UTC")

    assert result["status"] == "ok"
    assert result["ranked_hours"][0]["hour"] == 9
    assert result["ranked_hours"][0]["elevated_share"] == 1
    assert result["hours"][16]["elevated_share"] == 0
    assert result["hours"][8]["elevated_share"] is None
    assert result["hours"][9]["observed_days"] == 3


def test_gaps_do_not_count_as_low_stress_or_coverage():
    samples = [
        SimpleNamespace(ts=datetime(2026, 9, day, 9, minute, tzinfo=UTC), value=80)
        for day in (1, 2, 3)
        for minute in (0, 10, 20, 30, 40, 50)
    ]
    result = stress_by_hour(Session(samples), date(2026, 9, 1), date(2026, 9, 3), "UTC")

    assert result["status"] == "insufficient_coverage"
    assert result["ranked_hours"] == []
    assert result["hours"][9]["elevated_share"] is None


def test_repeated_daylight_saving_hour_uses_same_local_clock_label():
    segments = list(
        _split_local_hours(
            datetime(2026, 10, 25, 0, 55, tzinfo=UTC),
            datetime(2026, 10, 25, 1, 5, tzinfo=UTC),
            ZoneInfo("Europe/Bratislava"),
        )
    )
    assert segments == [(2, date(2026, 10, 25), 300), (2, date(2026, 10, 25), 300)]


def test_stress_hour_tool_requires_health_only():
    assert "analysis_stress_by_hour" in TOOLS
    assert TOOL_SCOPES["analysis_stress_by_hour"] == {"read:health"}


def test_stress_hour_tool_reads_saved_garmin_measurements(db):
    db.add_all(
        Measurement(
            ts=sample.ts,
            local_date=sample.ts.date(),
            metric="stress_score",
            source="garmin_connect",
            quality="observed",
            value=sample.value,
            unit="score",
        )
        for day in (1, 2, 3)
        for hour, score in ((9, 80), (16, 20))
        for sample in samples_for_hour(day, hour, score)
    )
    db.flush()

    result = call_tool(
        db,
        "analysis_stress_by_hour",
        {"start": "2026-09-01", "end": "2026-09-03", "timezone": "UTC"},
    )

    assert result["status"] == "ok"
    assert result["ranked_hours"][0]["hour"] == 9
    assert result["hours"][16]["elevated_share"] == 0


def test_stress_hour_window_is_bounded():
    with pytest.raises(ValueError):
        stress_by_hour(Session([]), date(2026, 8, 1), date(2026, 9, 1), "UTC")
