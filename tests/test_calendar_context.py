from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from garmin_ai.api import create_app
from garmin_ai.calendar_context import CalendarBatch, CalendarItem, import_batch, plans
from garmin_ai.config import ApiToken, CalendarSourceConsent, Settings
from garmin_ai.events import Conflict
from garmin_ai.models import AppState, Event

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def settings(source):
    return Settings(
        calendar_sources=[
            CalendarSourceConsent(
                id=source, categories={"work"}, granted_at=NOW - timedelta(days=1)
            )
        ]
    )


def item(source, **changes):
    return CalendarItem(
        **{
            "source_id": source,
            "id": uuid4(),
            "revision": 1,
            "status": "busy",
            "start": NOW,
            "end": NOW + timedelta(hours=1),
            "timezone": "UTC",
            "category": "work",
            **changes,
        }
    )


def test_disabled_source_does_not_write_or_imply_free_time(db):
    source = uuid4()
    with pytest.raises(ValueError, match="not enabled"):
        import_batch(db, Settings(), CalendarBatch(items=[item(source)]), NOW)
    result = plans(db, Settings(), NOW, NOW + timedelta(days=1), NOW)
    assert result["availability"] == "disabled" and result["rows"] == []
    assert not db.scalar(select(func.count()).select_from(AppState))


def test_plan_is_not_a_diary_fact_and_cancellation_survives_stale_delivery(db):
    source = uuid4()
    config = settings(source)
    original = item(source)
    batch = CalendarBatch(items=[original])
    assert import_batch(db, config, batch, NOW)["outcomes"][0]["status"] == "busy"
    assert import_batch(db, config, batch, NOW)["outcomes"][0]["status"] == "unchanged"
    result = plans(db, config, NOW, NOW + timedelta(days=1), NOW)
    assert len(result["rows"]) == 1 and result["evidence_type"] == "calendar_plan"
    assert db.scalar(select(func.count()).select_from(Event)) == 0
    cancel = CalendarItem(source_id=source, id=original.id, revision=2, status="cancelled")
    import_batch(db, config, CalendarBatch(items=[cancel]), NOW)
    assert import_batch(db, config, batch, NOW)["outcomes"][0]["status"] == "stale"
    assert plans(db, config, NOW, NOW + timedelta(days=1), NOW)["rows"] == []


def test_import_is_atomic_and_same_revision_conflicts(db):
    source = uuid4()
    config = settings(source)
    first = item(source)
    with pytest.raises(ValueError, match="not enabled"):
        import_batch(db, config, CalendarBatch(items=[first, item(uuid4())]), NOW)
    assert db.scalar(select(func.count()).select_from(AppState)) == 0
    import_batch(db, config, CalendarBatch(items=[first]), NOW)
    with pytest.raises(Conflict):
        import_batch(
            db,
            config,
            CalendarBatch(items=[first.model_copy(update={"end": NOW + timedelta(hours=2)})]),
            NOW,
        )


@pytest.mark.parametrize("field", ["title", "attendees", "description", "location", "url"])
def test_third_party_text_and_exact_location_are_rejected(field):
    with pytest.raises(ValidationError):
        CalendarItem(**{**item(uuid4()).model_dump(), field: "synthetic-private-text"})


def test_revocation_and_category_selection_hide_stored_plans(db):
    source = uuid4()
    config = settings(source)
    import_batch(db, config, CalendarBatch(items=[item(source)]), NOW)
    assert plans(db, Settings(), NOW, NOW + timedelta(days=1), NOW)["rows"] == []
    config.calendar_sources[0].categories = {"travel"}
    assert plans(db, config, NOW, NOW + timedelta(days=1), NOW)["rows"] == []
    with pytest.raises(ValueError, match="category"):
        import_batch(db, config, CalendarBatch(items=[item(source)]), NOW)


def test_timezones_and_half_open_boundaries_do_not_invent_attendance(db):
    source = uuid4()
    config = settings(source)
    entry = item(
        source,
        start=datetime.fromisoformat("2026-09-10T09:00:00+09:00"),
        end=datetime.fromisoformat("2026-09-10T10:00:00+09:00"),
        timezone="Asia/Tokyo",
    )
    import_batch(db, config, CalendarBatch(items=[entry]), NOW)
    assert (
        plans(db, config, NOW, NOW + timedelta(hours=1), NOW)["rows"][0]["start"] == NOW.isoformat()
    )
    assert plans(db, config, NOW + timedelta(hours=1), NOW + timedelta(hours=2), NOW)["rows"] == []


def test_calendar_api_requires_admin_and_explicit_source(db, db_engine):
    source = uuid4()
    config = settings(source)
    key = "synthetic-calendar-key-" + "x" * 32
    config.api_tokens = [ApiToken(key=key, scopes={"read:diary", "write:diary"})]
    client = TestClient(create_app(config, db_engine))
    headers = {"Authorization": "Bearer " + key}
    body = CalendarBatch(items=[item(source)]).model_dump(mode="json")
    assert client.post("/context/calendar/import", json=body, headers=headers).status_code == 403
    config.api_tokens = [ApiToken(key=key, scopes={"admin"})]
    assert client.post("/context/calendar/import", json=body, headers=headers).status_code == 200
    response = client.get(
        "/context/calendar",
        params={"start": NOW.isoformat(), "end": (NOW + timedelta(days=1)).isoformat()},
        headers=headers,
    )
    assert response.status_code == 200 and len(response.json()["rows"]) == 1


def test_conflicting_historical_revision_is_rejected(db):
    source = uuid4()
    config = settings(source)
    original = item(source)
    import_batch(db, config, CalendarBatch(items=[original]), NOW)
    import_batch(
        db, config, CalendarBatch(items=[original.model_copy(update={"revision": 2})]), NOW
    )
    with pytest.raises(Conflict):
        import_batch(
            db,
            config,
            CalendarBatch(items=[original.model_copy(update={"end": NOW + timedelta(hours=2)})]),
            NOW,
        )
    assert (
        import_batch(db, config, CalendarBatch(items=[original]), NOW)["outcomes"][0]["status"]
        == "stale"
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_source_rejected_independently_of_future_grant_order(db, reverse):
    source = uuid4()
    config = settings(source)
    future = CalendarSourceConsent(
        id=source, categories={"personal"}, granted_at=NOW + timedelta(days=1)
    )
    config.calendar_sources = [future, *config.calendar_sources]
    if reverse:
        config.calendar_sources.reverse()
    with pytest.raises(ValueError, match="Duplicate"):
        import_batch(db, config, CalendarBatch(items=[item(source)]), NOW)
    assert not db.scalar(select(func.count()).select_from(AppState))
