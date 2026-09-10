import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.diary_export import csv_cell, export_diary
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
KEY = "synthetic-diary-export-token-12345678"


@pytest.mark.parametrize("format", ["json", "csv"])
def test_diary_export_contains_only_explicit_diary_fields(db, db_engine, format):
    event = create_event(
        db,
        EventInput(
            start=NOW - timedelta(days=1),
            timezone="UTC",
            original_text="synthetic-original-message",
            payload={"type": "migraine", "severity": 3},
        ),
        actor="owner",
        idempotency_key="synthetic-internal-key",
    )
    db.add(AppState(key="synthetic-credentials", value={"token": "synthetic-secret-never-export"}))
    db.commit()
    client = TestClient(
        create_app(Settings(api_tokens=[ApiToken(key=KEY, scopes={"read:diary"})]), db_engine)
    )
    response = client.get(
        "/exports/diary",
        params={
            "start": NOW.isoformat(),
            "end": (NOW + timedelta(hours=1)).isoformat(),
            "timezone": "Asia/Tokyo",
            "format": format,
        },
        headers={"Authorization": "Bearer " + KEY},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    for hidden in (
        "synthetic-secret-never-export",
        "synthetic-original-message",
        "synthetic-internal-key",
        "synthetic-credentials",
    ):
        assert hidden not in response.text
    if format == "json":
        row = response.json()["rows"][0]
        assert row["id"] == str(event.id) and row["missing_end"] is True
        assert row["display_start"].endswith("+09:00")
        assert row["start"] == event.start.isoformat()
    else:
        row = list(csv.DictReader(io.StringIO(response.text)))[0]
        assert row["display_timezone"] == "Asia/Tokyo"
        assert row["display_end"] == ""
        assert json.loads(row["payload_json"])["severity"] == 3


@pytest.mark.parametrize(
    "scopes, expected", [({"read:health"}, 403), ({"write:diary"}, 403), ({"read:diary"}, 200)]
)
def test_export_requires_diary_read_scope(db, db_engine, scopes, expected):
    client = TestClient(
        create_app(Settings(api_tokens=[ApiToken(key=KEY, scopes=scopes)]), db_engine)
    )
    response = client.get(
        "/exports/diary",
        params={"start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()},
        headers={"Authorization": "Bearer " + KEY},
    )
    assert response.status_code == expected


def test_export_limits_are_explicit(db):
    with pytest.raises(ValueError):
        export_diary(db, NOW, NOW + timedelta(days=32), "UTC")
    with pytest.raises(ValueError):
        export_diary(db, NOW, NOW + timedelta(hours=1), "Unknown/Timezone")


@pytest.mark.parametrize("text", ["=1+1", " @SUM(A1)", "\t=1", "+command", "-command"])
def test_csv_cells_cannot_be_interpreted_as_formulas(text):
    assert csv_cell(text).startswith("'")
