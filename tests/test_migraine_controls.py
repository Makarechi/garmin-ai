from datetime import UTC, date, datetime, timedelta

import pytest

from garmin_ai.analytics import migraine_comparison
from garmin_ai.events import EventInput, create_event, delete_event, undo_last, update_event
from garmin_ai.models import HealthDay, Insight


def observation(db, start, end, headache="no", migraine="no", **kwargs):
    return create_event(
        db,
        EventInput(
            start=start,
            end=end,
            payload={"type": "headache_observation", "headache": headache, "migraine": migraine},
            **kwargs,
        ),
        actor="observation",
    )


def setup_days(db):
    for day in (1, 8, 15, 22, 29):
        db.add(HealthDay(day=date(2026, 9, day), sleep_score=70))
    db.flush()


def compare(db):
    return migraine_comparison(db, "sleep_score", date(2026, 9, 1), date(2026, 9, 1), "UTC")


@pytest.mark.parametrize("end", ["2026-09-10T00:00:00Z", None])
def test_controls_never_overlap_long_or_open_episode(db, end):
    setup_days(db)
    create_event(
        db,
        EventInput(start="2026-09-01T12:00:00Z", end=end, payload={"type": "migraine"}),
        actor="episode",
    )
    assert compare(db)["matched_pairs"] == 0
    observation(db, "2026-09-08T00:00:00Z", "2026-09-09T00:00:00Z")
    result = compare(db)
    assert result["matched_pairs"] == 0
    assert result["status"] == "insufficient_evidence"
    assert (
        next(d for d in result["control_days"] if d["day"] == "2026-09-08")["eligibility"]
        == "episode_exclusion"
    )


def test_old_open_episode_before_search_window_excludes_controls(db):
    setup_days(db)
    for start, end in [
        ("2025-01-01T12:00:00Z", None),
        ("2026-09-01T12:00:00Z", "2026-09-01T15:00:00Z"),
    ]:
        create_event(
            db, EventInput(start=start, end=end, payload={"type": "migraine"}), actor="episode"
        )
    observation(db, "2026-09-08T00:00:00Z", "2026-09-09T00:00:00Z")
    assert compare(db)["matched_pairs"] == 0


def test_negative_unknown_unanswered_and_partial_days_are_distinct(db):
    setup_days(db)
    create_event(
        db,
        EventInput(
            start="2026-09-01T12:00:00Z", end="2026-09-01T15:00:00Z", payload={"type": "migraine"}
        ),
        actor="episode",
    )
    observation(db, "2026-09-08T00:00:00Z", "2026-09-09T00:00:00Z")
    observation(db, "2026-09-15T00:00:00Z", "2026-09-16T00:00:00Z", "unknown", "unknown")
    observation(db, "2026-09-22T00:00:00Z", "2026-09-22T18:00:00Z")
    result = compare(db)
    assert [p["control_day"] for p in result["pairs"]] == ["2026-09-08"]
    coverage = {d["day"]: d["coverage"] for d in result["control_days"]}
    assert coverage["2026-09-08"] == "confirmed_negative"
    assert coverage["2026-09-15"] == "unknown"
    assert coverage["2026-09-22"] == "incomplete"
    assert coverage["2026-09-29"] == "unanswered"


def test_episode_correction_delete_undo_recomputes_and_supersedes_insight(db):
    setup_days(db)
    episode = create_event(
        db,
        EventInput(
            start="2026-09-01T12:00:00Z", end="2026-09-01T15:00:00Z", payload={"type": "migraine"}
        ),
        actor="episode",
    )
    observation(db, "2026-09-08T00:00:00Z", "2026-09-09T00:00:00Z")
    assert compare(db)["matched_pairs"] == 1
    insight = Insight(
        category="migraine_comparison",
        statement="synthetic",
        evidence={},
        sample_size=1,
        status="candidate",
        dedup_key="synthetic",
    )
    db.add(insight)
    db.flush()
    update_event(
        db,
        episode.id,
        EventInput(start=episode.start, end="2026-09-10T15:00:00Z", payload={"type": "migraine"}),
        revision=1,
        actor="episode",
    )
    assert compare(db)["matched_pairs"] == 0
    db.refresh(insight)
    assert insight.status == "superseded"
    undo_last(db, actor="episode")
    assert compare(db)["matched_pairs"] == 1
    delete_event(db, episode.id, revision=episode.revision, actor="episode")
    assert compare(db)["episodes"] == 0


@pytest.mark.parametrize("hours", [23, 25])
def test_full_day_coverage_joins_adjacent_intervals(db, hours):
    from garmin_ai.analytics import headache_day_coverage

    start = datetime(2026, 3, 29, tzinfo=UTC)
    middle = start + timedelta(hours=12)
    end = start + timedelta(hours=hours)
    rows = [observation(db, start, middle), observation(db, middle, end)]
    assert headache_day_coverage(rows, start, end) == "confirmed_negative"
    rows.append(observation(db, middle, end, headache="yes"))
    assert headache_day_coverage(rows, start, end) == "positive"


@pytest.mark.parametrize("status", ["inferred", "needs_confirmation"])
def test_unconfirmed_observations_cannot_establish_negative_days(db, status):
    setup_days(db)
    observation(db, "2026-09-08T00:00:00Z", "2026-09-09T00:00:00Z", status=status)
    assert (
        next(d for d in compare(db)["control_days"] if d["day"] == "2026-09-08")["coverage"]
        == "unanswered"
    )


@pytest.mark.parametrize("end", [None, "2026-09-01T00:00:00Z"])
def test_observations_require_covered_interval(end):
    with pytest.raises(ValueError, match="nonempty"):
        EventInput(
            start="2026-09-01T00:00:00Z",
            end=end,
            payload={"type": "headache_observation", "headache": "no", "migraine": "no"},
        )


@pytest.mark.parametrize("status", ["inferred", "needs_confirmation"])
@pytest.mark.parametrize("value", ["yes", "unknown"])
def test_unconfirmed_symptoms_veto_confirmed_negative_controls(db, status, value):
    setup_days(db)
    observation(db, "2026-09-08T00:00:00Z", "2026-09-09T00:00:00Z")
    observation(db, "2026-09-08T12:00:00Z", "2026-09-08T13:00:00Z", headache=value, status=status)
    result = compare(db)
    day = next(d for d in result["control_days"] if d["day"] == "2026-09-08")
    assert day["coverage"] == ("positive" if value == "yes" else "unknown")
    assert day["eligibility"] != "confirmed_control"


def test_year_comparison_keeps_statistics_in_model_budget(db):
    import json

    from garmin_ai.llm import compact

    start = date(2025, 1, 1)
    for offset in range(365):
        db.add(HealthDay(day=start + timedelta(days=offset), sleep_score=70))
    db.flush()
    result = migraine_comparison(db, "sleep_score", start, start + timedelta(days=364), "UTC")
    delivered = json.loads(compact(result))
    assert delivered["matched_pairs"] == 0
    assert delivered["control_days_total"] == 365
    assert delivered["control_coverage_counts"] == {"unanswered": 365}
    assert delivered["control_days_truncated"]
    assert len(delivered["control_days"]) == 50


@pytest.mark.parametrize("value,label", [("yes", "да"), ("no", "нет"), ("unknown", "неизвестно")])
def test_observation_confirmation_and_history_show_symptoms(db, value, label):
    from sqlalchemy import select

    from garmin_ai.agent import Interpretation, apply_command
    from garmin_ai.models import Event
    from garmin_ai.telegram import diary_label

    now = datetime(2026, 9, 9, tzinfo=UTC)
    command = Interpretation(
        intent="log",
        confidence=1,
        events=[
            EventInput(
                start=now - timedelta(days=1),
                end=now,
                payload={"type": "headache_observation", "headache": value, "migraine": value},
            )
        ],
    )
    reply = apply_command(db, command, text="synthetic", update_id=123, actor="owner", now=now)
    row = db.scalar(select(Event).where(Event.kind == "headache_observation"))
    for rendered in (reply, diary_label(row)):
        assert f"Головная боль: {label}" in rendered
        assert f"мигрень: {label}" in rendered
        assert "headache_observation" not in rendered
