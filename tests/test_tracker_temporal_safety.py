"""Synthetic counterexamples for calendar evidence, independent of a real model."""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.llm import ProviderUnavailable
from garmin_ai.models import Audit, Event, MetricObservation, TimelineInterval
from garmin_ai.natural_language import (
    _datetime_evidence,
    _datetime_is_evidenced,
    process_tracker_text,
)
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

NOW = datetime(2026, 10, 22, 14, tzinfo=UTC)


@pytest.mark.parametrize("quote", ["včera o 14:00", "ayer a las 14:00", "14:00", "at 14:00"])
@pytest.mark.parametrize("day_offset", [-1, 0])
def test_clock_cannot_supply_an_unverified_calendar_day(quote, day_offset):
    assert not _datetime_is_evidenced(NOW + timedelta(days=day_offset), quote, "UTC", NOW)


@pytest.mark.parametrize(
    ("quote", "day_offset"),
    [
        ("today at 14:00", 0),
        ("сегодня в 14:00", 0),
        ("yesterday at 14:00", -1),
        ("вчера в 14:00", -1),
        ("2026-10-21 at 14:00", -1),
        ("21.10.2026 в 14:00", -1),
    ],
)
def test_supported_calendar_basis_preserves_the_day(quote, day_offset):
    assert _datetime_is_evidenced(NOW + timedelta(days=day_offset), quote, "UTC", NOW)
    assert not _datetime_is_evidenced(NOW + timedelta(days=day_offset + 1), quote, "UTC", NOW)


@pytest.mark.parametrize("quote", ["unknown", "snow", "nowhere", "сейчаснет"])
def test_now_requires_a_complete_word(quote):
    assert not _datetime_is_evidenced(NOW, quote, "UTC", NOW)


@pytest.mark.parametrize("quote", ["now", "just now", "сейчас", "только что"])
def test_now_uses_elapsed_time_across_local_midnight(quote):
    anchor = datetime(2026, 10, 22, 0, 0, 30, tzinfo=UTC)
    assert _datetime_is_evidenced(anchor - timedelta(seconds=45), quote, "UTC", anchor)
    assert not _datetime_is_evidenced(anchor - timedelta(days=1), quote, "UTC", anchor)


@pytest.mark.parametrize("quote", ["today at about 14:00", "сегодня примерно в 14:00"])
def test_approximate_clock_does_not_prove_an_exact_instant(quote):
    assert not _datetime_is_evidenced(NOW, quote, "UTC", NOW)


def test_nonexistent_spring_clock_is_not_silently_shifted():
    anchor = datetime(2026, 3, 29, 12, tzinfo=UTC)
    shifted = datetime.fromisoformat("2026-03-29T02:30:00+01:00")
    assert not _datetime_is_evidenced(shifted, "today at 02:30", "Europe/Bratislava", anchor)


def test_relative_date_uses_message_local_day_across_midnight():
    anchor = datetime(2026, 10, 21, 22, 5, tzinfo=UTC)
    yesterday = datetime.fromisoformat("2026-10-21T14:00:00+02:00")
    assert _datetime_is_evidenced(yesterday, "yesterday at 14:00", "Europe/Bratislava", anchor)


@pytest.mark.parametrize(
    ("quote", "status", "basis"),
    [
        ("2026-10-22 at 14:00", "supported", "explicit_date"),
        ("today at 14:00", "supported", "relative_day"),
        ("now", "supported", "message_time"),
        ("včera o 14:00", "unverified", None),
        ("yesterday at 14:00", "contradictory", None),
        ("today, yesterday at 14:00", "ambiguous", None),
        ("yesterday 2026-10-22 at 14:00", "ambiguous", None),
        ("today at 14:00, actually yesterday", "ambiguous", None),
        ("yesterday at 14:00, today at 14:00", "ambiguous", None),
    ],
)
def test_calendar_result_distinguishes_basis_and_failure_kind(quote, status, basis):
    result = _datetime_evidence(NOW, quote, "UTC", NOW)
    assert result.status == status
    assert result.date_basis == basis


def test_clipped_clock_cannot_borrow_a_day_from_another_fact():
    result = _datetime_evidence(NOW, "14:00", "UTC", NOW, text="Today rating 3; včera o 14:00")
    assert result.status == "unverified"


def test_each_relative_day_stays_with_its_clock():
    quote = "yesterday at 14:00, today at 15:00"
    yesterday = NOW - timedelta(days=1)
    assert _datetime_is_evidenced(yesterday, quote, "UTC", NOW)
    assert not _datetime_is_evidenced(NOW, quote, "UTC", NOW)


def test_dst_offset_must_belong_to_the_proposed_clock():
    anchor = datetime(2026, 10, 25, 12, tzinfo=UTC)
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert not _datetime_is_evidenced(
        first, "today at 01:30 +02:00, at 02:30 +01:00", "Europe/Bratislava", anchor
    )


def test_now_tolerance_uses_elapsed_time_during_fall_back():
    zone = ZoneInfo("Europe/Bratislava")
    anchor = datetime(2026, 10, 25, 1, 30, tzinfo=UTC).astimezone(zone)
    an_hour_before = datetime(2026, 10, 25, 0, 30, tzinfo=UTC).astimezone(zone)
    assert not _datetime_is_evidenced(an_hour_before, "now", "Europe/Bratislava", anchor)


@pytest.mark.parametrize(
    "quote",
    ["Today at 14:00 I walked around the park", "Сегодня в 14:00 гулял около дома"],
)
def test_non_temporal_approximation_words_preserve_an_exact_clock(quote):
    assert _datetime_is_evidenced(NOW, quote, "UTC", NOW)


@pytest.mark.parametrize("hour", [11, 12])
@pytest.mark.parametrize(
    "quote",
    [
        "today from 10:00 to 11:00 tomorrow, reminder at 12:00",
        "today from 10:00 to 11:00 tomorrow at 12:00",
    ],
)
def test_mixed_date_order_cannot_lend_the_wrong_day(hour, quote):
    assert not _datetime_is_evidenced(NOW.replace(hour=hour), quote, "UTC", NOW)


@pytest.mark.parametrize("utc_hour", [0, 1])
def test_each_repeated_clock_uses_its_own_explicit_offset(utc_hour):
    anchor = datetime(2026, 10, 25, 12, tzinfo=UTC)
    value = datetime(2026, 10, 25, utc_hour, 30, tzinfo=UTC)
    quote = "today from 02:30+02:00 to 02:30+01:00"
    assert _datetime_is_evidenced(value, quote, "Europe/Bratislava", anchor)


@pytest.mark.parametrize(("offset", "expected"), [("+00:00", True), ("+01:00", False)])
@pytest.mark.parametrize("day", ["today", "now"])
def test_explicit_offset_cannot_be_ignored_on_an_ordinary_day(offset, expected, day):
    assert _datetime_is_evidenced(NOW, day + " at 14:00" + offset, "UTC", NOW) is expected


def test_mixed_order_preserves_each_endpoints_own_day():
    quote = "today from 10:00 to 11:00 tomorrow, reminder at 12:00"
    assert _datetime_is_evidenced(NOW.replace(hour=10), quote, "UTC", NOW)
    assert _datetime_is_evidenced((NOW + timedelta(days=1)).replace(hour=11), quote, "UTC", NOW)
    assert not _datetime_is_evidenced((NOW + timedelta(days=1)).replace(hour=12), quote, "UTC", NOW)


def test_explicit_dates_after_clocks_are_bound_individually():
    quote = "14:00 on 2026-10-21, then 15:00 on 2026-10-22"
    assert _datetime_is_evidenced(NOW - timedelta(days=1), quote, "UTC", NOW)
    assert not _datetime_is_evidenced(NOW, quote, "UTC", NOW)
    assert _datetime_is_evidenced(NOW.replace(hour=15), quote, "UTC", NOW)


@pytest.mark.parametrize("quote", ["today from 14:00 until 14:15", "сегодня с 14:00 по 14:15"])
def test_supported_interval_connectors_carry_the_day_to_the_end(quote):
    assert _datetime_is_evidenced(NOW + timedelta(minutes=15), quote, "UTC", NOW)


@pytest.mark.parametrize(
    ("text", "quote"),
    [
        ("Rating 3: about today at 14:00", "today at 14:00"),
        ("Rating 3: today at about 14:00", "14:00"),
    ],
)
def test_clipped_quote_cannot_hide_an_approximate_temporal_modifier(text, quote):
    result = _datetime_evidence(NOW, quote, "UTC", NOW, text=text, quote_start=text.index(quote))
    assert result.status == "ambiguous"
    assert result.reason == "approximate_time"


def test_unrelated_day_in_prose_does_not_prove_the_first_clocks_day():
    quote = "My birthday is today. Log pain at 10:00; tomorrow at 11:00 is my appointment"
    result = _datetime_evidence(NOW.replace(hour=10), quote, "UTC", NOW)
    assert result.status == "unverified"


@pytest.mark.parametrize("quote", ["today between 14:00 and 15:00", "сегодня между 14:00 и 15:00"])
def test_between_and_interval_has_calendar_evidence_for_both_ends(quote):
    assert _datetime_is_evidenced(NOW, quote, "UTC", NOW)
    assert _datetime_is_evidenced(NOW.replace(hour=15), quote, "UTC", NOW)


@pytest.mark.parametrize("quote", ["today 14:00-15:00", "today 14:00 - 15:00", "today 14.00-15.00"])
def test_compact_ranges_have_two_clocks_in_bounded_interval_context(quote):
    for hour in (14, 15):
        result = _datetime_evidence(
            NOW.replace(hour=hour), quote, "UTC", NOW, interval_hyphens=True
        )
        assert result.status == "supported"


@pytest.mark.parametrize("suffix", [".", ":", ",", "!"])
@pytest.mark.parametrize("phrase", ["Today at 14:00", "Сегодня в 14:00", "Today at 14"])
def test_terminal_punctuation_preserves_an_exact_clock(phrase, suffix):
    assert _datetime_is_evidenced(NOW, phrase + suffix, "UTC", NOW)


@pytest.mark.parametrize("phrase", ["today at 14:00:30", "today at 14.00.30", "today at 14:001"])
def test_additional_numeric_precision_cannot_be_clipped_to_minutes(phrase):
    assert not _datetime_is_evidenced(NOW, phrase, "UTC", NOW)


@pytest.mark.parametrize("offset", ["Z", "+00:00", "-04:00"])
def test_compact_interval_endpoints_keep_their_explicit_offsets(offset):
    timezone = "America/New_York" if offset == "-04:00" else "UTC"
    quote = f"today 14:00{offset}-15:00{offset}"
    for hour in (14, 15):
        value = NOW.replace(hour=hour, tzinfo=ZoneInfo(timezone))
        result = _datetime_evidence(
            value, quote, timezone, NOW, interval_hyphens=True, interval_clocks=((14, 0), (15, 0))
        )
        assert result.status == "supported"


def test_offset_in_a_compact_interval_is_not_a_proposed_endpoint():
    quote = "today 14:00-04:00-15:00-04:00"
    result = _datetime_evidence(
        NOW.replace(hour=4),
        quote,
        "UTC",
        NOW,
        interval_hyphens=True,
        interval_clocks=((14, 0), (4, 0)),
    )
    assert result.status != "supported"


@pytest.mark.parametrize("quote", ["today at 14:00 UTC-04:00", "2026-10-22T14:00-04:00"])
def test_explicit_negative_offsets_are_not_interval_end_clocks(quote):
    start = datetime(2026, 10, 22, 18, tzinfo=UTC)
    false_end = datetime(2026, 10, 22, 8, tzinfo=UTC)
    assert (
        _datetime_evidence(start, quote, "America/New_York", NOW, interval_hyphens=True).status
        == "supported"
    )
    assert (
        _datetime_evidence(false_end, quote, "America/New_York", NOW, interval_hyphens=True).status
        == "unverified"
    )


def test_point_clock_keeps_its_attached_negative_offset():
    quote = "today at 14:00-04:00"
    assert _datetime_is_evidenced(NOW.replace(hour=18), quote, "America/New_York", NOW)
    assert not _datetime_is_evidenced(NOW.replace(hour=14), quote, "UTC", NOW)
    assert (
        _datetime_evidence(
            NOW.replace(hour=18),
            quote,
            "America/New_York",
            NOW,
            interval_hyphens=True,
            interval_clocks=((14, 0), (20, 0)),
        ).status
        == "supported"
    )


@pytest.mark.parametrize(
    "quote",
    [
        "Today, at 14:00 I walked",
        "Сегодня, в 14:00 я гулял",
        "today, 14:00",
        "today: at 14:00",
        "14:00, today",
        "14:00, on 2026-10-22",
    ],
)
def test_punctuation_does_not_separate_an_exact_date_and_clock(quote):
    assert _datetime_is_evidenced(NOW, quote, "UTC", NOW)


@pytest.mark.parametrize(
    "quote",
    ["Today. At 14:00", "Today; at 14:00", "Today, I walked at 14:00"],
)
def test_punctuation_does_not_let_a_date_cross_a_clause(quote):
    assert not _datetime_is_evidenced(NOW, quote, "UTC", NOW)


def test_punctuated_clauses_keep_each_clocks_day():
    quote = "yesterday, at 14:00Z, today, at 15:00Z"
    assert _datetime_is_evidenced(NOW - timedelta(days=1), quote, "UTC", NOW)
    assert _datetime_is_evidenced(NOW.replace(hour=15), quote, "UTC", NOW)
    assert not _datetime_is_evidenced(NOW, quote, "UTC", NOW)


def test_offset_colon_is_not_a_clause_separator_before_a_conflicting_day():
    quote = "today from 10:00+00:00 to 11:00+00:00 tomorrow at 12:00"
    assert not _datetime_is_evidenced(NOW.replace(hour=11), quote, "UTC", NOW)


@pytest.mark.parametrize("suffix", ["Z", " UTC", " GMT", " utc", " gmt"])
@pytest.mark.parametrize("timezone", ["UTC", "Europe/Bratislava"])
def test_zero_offset_designator_cannot_be_ignored(suffix, timezone):
    value = NOW.replace(tzinfo=ZoneInfo(timezone))
    result = _datetime_evidence(value, "2026-10-22T14:00" + suffix, timezone, NOW)
    assert result.status == ("supported" if timezone == "UTC" else "contradictory")


@pytest.mark.parametrize("quote", ["14:00Z on 2026-10-22", "14:00 UTC today"])
def test_zero_offset_designator_preserves_a_following_calendar_day(quote):
    assert _datetime_is_evidenced(NOW, quote, "UTC", NOW)


def test_zero_offset_designator_preserves_an_interval_endpoints_day():
    quote = "today from 14:00Z to 15:00Z"
    assert _datetime_is_evidenced(NOW, quote, "UTC", NOW)
    assert _datetime_is_evidenced(NOW.replace(hour=15), quote, "UTC", NOW)


def test_plain_and_does_not_copy_a_day_to_an_unrelated_clock():
    assert not _datetime_is_evidenced(NOW.replace(hour=15), "today 14:00 and 15:00", "UTC", NOW)


@pytest.fixture
def temporal_tracker(db, request):
    draft = TrackerSetupDraft(
        key="timing",
        name="Timing",
        topology=getattr(request, "param", "point"),
        fields=[
            TrackerFieldDraft(key="rating", label="Rating", kind="scale", minimum=1, maximum=5)
        ],
        shortcut="Record timing",
    )
    preview = preview_tracker(db, draft)
    result = confirm_tracker(
        db,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    return result["action"]["definition_version_id"]


class TemporalProvider:
    def __init__(self, version_id, text, quote, start):
        self.prompts = []
        self.closed = False
        self.result = {
            "schema_version": "tracker.nl.v1",
            "intent": "create_entry",
            "definition_version_id": version_id,
            "start": start.isoformat(),
            "start_evidence": {
                "start": text.index(quote),
                "end": text.index(quote) + len(quote),
                "quote": quote,
            },
            "fields": [
                {
                    "field_id": "user.timing.rating",
                    "value": 3,
                    "evidence": {"start": 7, "end": 8, "quote": "3"},
                }
            ],
            "confidence": 1,
        }

    def structured(self, _instruction, prompt, schema):
        self.prompts.append(prompt)
        return schema.model_validate(self.result)

    def close(self):
        self.closed = True


def fact_counts(session):
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (Event, Audit, MetricObservation, TimelineInterval)
    )


@pytest.mark.parametrize(
    "phrase",
    ["Today, at 14:00 I walked", "Сегодня, в 14:00 я гулял", "Today at 14:00.", "Сегодня в 14:00."],
)
def test_punctuated_temporal_phrase_is_written(db, temporal_tracker, phrase):
    text = "Rating 3: " + phrase
    result = process_tracker_text(
        db,
        TemporalProvider(temporal_tracker, text, phrase, NOW),
        {"text": text, "operation_id": "punctuated-time"},
        granted={"read:diary", "write:diary"},
        actor="test",
        timezone="UTC",
        now=NOW,
    )
    db.commit()
    assert result["written"] is True
    assert db.get(Event, UUID(result["event_id"])).start == NOW


@pytest.mark.parametrize("suffix", ["Z", " UTC", " GMT"])
def test_zero_offset_cannot_write_a_different_local_instant(db, temporal_tracker, suffix):
    quote = "2026-10-22T14:00" + suffix
    text = "Rating 3: " + quote
    before = fact_counts(db)
    result = process_tracker_text(
        db,
        TemporalProvider(
            temporal_tracker, text, quote, NOW.replace(tzinfo=ZoneInfo("Europe/Bratislava"))
        ),
        {"text": text, "operation_id": "zero-offset-mismatch"},
        granted={"read:diary", "write:diary"},
        actor="test",
        timezone="Europe/Bratislava",
        now=NOW,
    )
    db.commit()
    assert result["intent"] == "clarify"
    assert result["written"] is False
    assert result["temporal_evidence"]["reason"] == "clock_offset_mismatch"
    assert fact_counts(db) == before


@pytest.mark.parametrize("phrase", ["včera o 14:00", "ayer a las 14:00"])
@pytest.mark.parametrize("clipped", [False, True])
@pytest.mark.parametrize("day_offset", [-1, 0])
def test_unverified_calendar_expression_clarifies_without_any_fact(
    db, temporal_tracker, phrase, clipped, day_offset
):
    text = "Rating 3: " + phrase
    provider = TemporalProvider(
        temporal_tracker, text, "14:00" if clipped else phrase, NOW + timedelta(days=day_offset)
    )
    before = fact_counts(db)

    result = process_tracker_text(
        db,
        provider,
        {"text": text, "operation_id": "unverified-calendar"},
        granted={"read:diary", "write:diary"},
        actor="test",
        timezone="UTC",
        now=NOW,
    )
    db.commit()

    assert result["intent"] == "clarify"
    assert result["written"] is False
    assert result["temporal_evidence"]["status"] == "unverified"
    assert fact_counts(db) == before


@pytest.mark.parametrize("timezone", ["UTC", "Europe/Bratislava"])
def test_http_unverified_date_clarifies_and_commits_no_fact(
    db, db_engine, temporal_tracker, monkeypatch, timezone
):
    text = "Rating 3: ayer a las 14:00"
    proposed = NOW.replace(tzinfo=ZoneInfo(timezone))
    provider = TemporalProvider(temporal_tracker, text, "14:00", proposed)
    before = fact_counts(db)
    db.commit()

    class ApiClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz or UTC)

    monkeypatch.setattr("garmin_ai.api.datetime", ApiClock)
    monkeypatch.setattr("garmin_ai.onboarding.model_category_selected", lambda *_args: True)
    monkeypatch.setattr("garmin_ai.integrations.create_model_provider", lambda *_args: provider)
    token = "synthetic-ma01-http-" + "x" * 32
    settings = Settings(
        _env_file=None,
        timezone=timezone,
        llm_enabled=True,
        gemini_model="synthetic",
        integrations=[{"id": "model:gemini:synthetic", "kind": "model", "provider": "gemini"}],
        api_tokens=[ApiToken(key=token, scopes={"read:diary", "write:diary"})],
    )

    response = TestClient(create_app(settings, db_engine)).post(
        "/natural-language/trackers",
        headers={"Authorization": "Bearer " + token},
        json={"text": text, "operation_id": "http-unverified-calendar"},
    )

    assert response.status_code == 200
    assert response.json()["intent"] == "clarify"
    assert response.json()["written"] is False
    assert response.json()["temporal_evidence"]["reason"] == "calendar_day_not_evidenced"
    assert provider.closed
    with Session(db_engine) as fresh:
        assert fact_counts(fresh) == before


@pytest.mark.parametrize("first_attempt", ["clarify", "unavailable", "offline"])
def test_retried_interpretation_keeps_original_day_and_timezone(
    db, db_engine, temporal_tracker, first_attempt
):
    import json

    text = "Rating 3: yesterday at 14:00"
    sent = datetime(2026, 10, 21, 22, 5, tzinfo=UTC)
    start = datetime.fromisoformat("2026-10-21T14:00:00+02:00")
    request = {"text": text, "operation_id": "retry-after-midnight"}

    class ClarifyProvider:
        def structured(self, _instruction, _prompt, schema):
            if first_attempt == "unavailable":
                raise ProviderUnavailable("synthetic outage")
            return schema(schema_version="tracker.nl.v1", intent="clarify", confidence=1)

    process_tracker_text(
        db,
        None if first_attempt == "offline" else ClarifyProvider(),
        request,
        granted={"read:diary", "write:diary"},
        actor="test",
        now=sent,
        timezone="Europe/Bratislava",
    )
    db.commit()
    provider = TemporalProvider(temporal_tracker, text, "yesterday at 14:00", start)
    with Session(db_engine) as restarted, restarted.begin():
        result = process_tracker_text(
            restarted,
            provider,
            request,
            granted={"read:diary", "write:diary"},
            actor="test",
            now=sent + timedelta(days=1),
            timezone="America/New_York",
        )
        assert result["written"] is True
        event = restarted.get(Event, UUID(result["event_id"]))
        assert event.start == start
        assert event.timezone == "Europe/Bratislava"
    prompt = json.loads(provider.prompts[0])
    assert datetime.fromisoformat(prompt["now"]) == sent
    assert prompt["timezone"] == "Europe/Bratislava"


@pytest.mark.parametrize("temporal_tracker", ["bounded_interval"], indirect=True)
def test_unverified_end_cannot_leave_a_partially_written_interval(db, temporal_tracker):
    text = "Rating 3: today at 14:00 to 14:15"
    provider = TemporalProvider(temporal_tracker, text, "today at 14:00", NOW)
    provider.result.update(
        end=(NOW + timedelta(minutes=15)).isoformat(),
        end_evidence={"start": text.index("14:15"), "end": len(text), "quote": "14:15"},
    )
    before = fact_counts(db)
    result = process_tracker_text(
        db,
        provider,
        {"text": text, "operation_id": "unverified-end"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=NOW,
    )
    db.commit()
    assert result["intent"] == "clarify"
    assert result["temporal_evidence"]["field"] == "end"
    assert fact_counts(db) == before


def test_unverified_correction_leaves_original_revision_and_projections(db, temporal_tracker):
    text = "Rating 3: today at 14:00"
    created = process_tracker_text(
        db,
        TemporalProvider(temporal_tracker, text, "today at 14:00", NOW),
        {"text": text, "operation_id": "correction-base"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=NOW,
    )
    db.commit()
    before = fact_counts(db)
    text = "Rating 3: včera o 14:00"
    provider = TemporalProvider(temporal_tracker, text, "14:00", NOW - timedelta(days=1))
    provider.result.update(intent="update_entry", event_id=created["event_id"])
    result = process_tracker_text(
        db,
        provider,
        {
            "text": text,
            "operation_id": "unverified-correction",
            "selected_event_id": created["event_id"],
        },
        granted={"read:diary", "write:diary"},
        actor="test",
        now=NOW,
    )
    db.commit()
    assert result["written"] is False
    assert fact_counts(db) == before
    event = db.get(Event, UUID(created["event_id"]))
    assert event.start == NOW
    assert event.revision == created["revision"]


@pytest.mark.parametrize(
    "quote",
    [
        "today from 10:00 to 11:00 tomorrow, reminder at 12:00",
        "today from 10:00+00:00 to 11:00+00:00 tomorrow at 12:00",
    ],
)
def test_mixed_date_order_clarifies_without_writing_the_wrong_day(db, temporal_tracker, quote):
    text = "Rating 3: " + quote
    before = fact_counts(db)
    result = process_tracker_text(
        db,
        TemporalProvider(temporal_tracker, text, quote, NOW.replace(hour=11)),
        {"text": text, "operation_id": "mixed-date-order"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=NOW,
    )
    db.commit()
    assert result["intent"] == "clarify"
    assert result["written"] is False
    assert fact_counts(db) == before


@pytest.mark.parametrize(
    "quote", ["today from 02:30+02:00 to 02:30+01:00", "today 02:30+02:00-02:30+01:00"]
)
@pytest.mark.parametrize("temporal_tracker", ["bounded_interval"], indirect=True)
def test_full_quote_can_record_interval_across_repeated_dst_hour(db, temporal_tracker, quote):
    text = "Rating 3: " + quote
    start = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    end = start + timedelta(hours=1)
    provider = TemporalProvider(temporal_tracker, text, quote, start)
    provider.result.update(end=end.isoformat(), end_evidence=provider.result["start_evidence"])
    result = process_tracker_text(
        db,
        provider,
        {"text": text, "operation_id": "repeated-dst-interval"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=datetime(2026, 10, 25, 12, tzinfo=UTC),
        timezone="Europe/Bratislava",
    )
    db.commit()
    assert result["written"] is True
    event = db.get(Event, UUID(result["event_id"]))
    assert (event.start, event.end) == (start, end)


@pytest.mark.parametrize(
    "phrase", ["Today at 14:00 I walked around the park", "Сегодня в 14:00 гулял около дома"]
)
def test_exact_entry_with_non_temporal_approximation_word_is_written(db, temporal_tracker, phrase):
    text = "Rating 3: " + phrase
    result = process_tracker_text(
        db,
        TemporalProvider(temporal_tracker, text, phrase, NOW),
        {"text": text, "operation_id": "exact-clock-around-park"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=NOW,
    )
    db.commit()
    assert result["written"] is True
    assert db.get(Event, UUID(result["event_id"])).start == NOW


@pytest.mark.parametrize(
    "quote",
    [
        "today 14:00-15:00",
        "today between 14:00 and 15:00",
        "today 14:00Z-15:00Z",
        "today 14:00+00:00-15:00+00:00",
    ],
)
@pytest.mark.parametrize("temporal_tracker", ["bounded_interval"], indirect=True)
def test_exact_compact_and_between_intervals_are_written(db, temporal_tracker, quote):
    text = "Rating 3: " + quote
    provider = TemporalProvider(temporal_tracker, text, quote, NOW)
    end = NOW + timedelta(hours=1)
    provider.result.update(end=end.isoformat(), end_evidence=provider.result["start_evidence"])
    result = process_tracker_text(
        db,
        provider,
        {"text": text, "operation_id": "explicit-interval"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=end,
    )
    db.commit()
    assert result["written"] is True
    event = db.get(Event, UUID(result["event_id"]))
    assert (event.start, event.end) == (NOW, end)


def test_unrelated_day_in_another_clause_never_creates_a_fact(db, temporal_tracker):
    quote = "My birthday is today. Log pain at 10:00; tomorrow at 11:00 is my appointment"
    text = "Rating 3: " + quote
    before = fact_counts(db)
    result = process_tracker_text(
        db,
        TemporalProvider(temporal_tracker, text, quote, NOW.replace(hour=10)),
        {"text": text, "operation_id": "unrelated-day"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=NOW,
    )
    db.commit()
    assert result["intent"] == "clarify"
    assert result["written"] is False
    assert fact_counts(db) == before


@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize("temporal_tracker", ["bounded_interval"], indirect=True)
def test_partial_interval_correction_cannot_discard_a_negative_offset(db, temporal_tracker, field):
    quote = "today 12:00 to 20:00"
    text = "Rating 3: " + quote
    start, end = NOW.replace(hour=12), NOW.replace(hour=20)
    provider = TemporalProvider(temporal_tracker, text, quote, start)
    provider.result.update(end=end.isoformat(), end_evidence=provider.result["start_evidence"])
    created = process_tracker_text(
        db,
        provider,
        {"text": text, "operation_id": "offset-base"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=end,
    )
    db.commit()
    before = fact_counts(db)
    quote = "today at 14:00-04:00"
    text = "Rating 3: " + quote
    provider = TemporalProvider(temporal_tracker, text, quote, NOW)
    provider.result.update(intent="update_entry", event_id=created["event_id"])
    if field == "end":
        provider.result["end"] = provider.result.pop("start")
        provider.result["end_evidence"] = provider.result.pop("start_evidence")
    result = process_tracker_text(
        db,
        provider,
        {"text": text, "operation_id": "offset-partial", "selected_event_id": created["event_id"]},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=end,
    )
    db.commit()
    assert result["written"] is False
    assert fact_counts(db) == before
    event = db.get(Event, UUID(created["event_id"]))
    assert (event.start, event.end, event.revision) == (start, end, created["revision"])


@pytest.mark.parametrize("temporal_tracker", ["bounded_interval"], indirect=True)
def test_unconsumed_offset_clock_cannot_fake_a_different_proposed_interval(db, temporal_tracker):
    quote = "today at 14:00-04:00, today at 20:00"
    text = "Rating 3: " + quote
    provider = TemporalProvider(temporal_tracker, text, quote, NOW)
    provider.result.update(
        end=NOW.replace(hour=20).isoformat(), end_evidence=provider.result["start_evidence"]
    )
    before = fact_counts(db)
    result = process_tracker_text(
        db,
        provider,
        {"text": text, "operation_id": "offset-mismatched-range"},
        granted={"read:diary", "write:diary"},
        actor="test",
        now=NOW.replace(hour=20),
    )
    db.commit()
    assert result["written"] is False
    assert fact_counts(db) == before
