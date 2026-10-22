"""Synthetic counterexamples for calendar evidence, independent of a real model."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

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
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Bratislava")
    anchor = datetime(2026, 10, 25, 1, 30, tzinfo=UTC).astimezone(zone)
    an_hour_before = datetime(2026, 10, 25, 0, 30, tzinfo=UTC).astimezone(zone)
    assert not _datetime_is_evidenced(an_hour_before, "now", "Europe/Bratislava", anchor)


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


def test_http_unverified_date_clarifies_and_commits_no_fact(
    db, db_engine, temporal_tracker, monkeypatch
):
    text = "Rating 3: ayer a las 14:00"
    provider = TemporalProvider(temporal_tracker, text, "14:00", NOW)
    before = fact_counts(db)
    db.commit()
    monkeypatch.setattr("garmin_ai.onboarding.model_category_selected", lambda *_args: True)
    monkeypatch.setattr("garmin_ai.integrations.create_model_provider", lambda *_args: provider)
    token = "synthetic-ma01-http-" + "x" * 32
    settings = Settings(
        _env_file=None,
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
