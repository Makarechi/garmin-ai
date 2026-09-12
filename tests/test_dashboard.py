from fastapi.testclient import TestClient

from garmin_ai.api import create_app
from garmin_ai.config import Settings

KEY = "synthetic-dashboard-token-00000000"


def test_public_dashboard_is_only_shell_and_assets(db_engine):
    client = TestClient(create_app(Settings(api_key=KEY), db_engine))
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "Демонстрационные данные" in response.text
    assert KEY not in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/dashboard-assets/app.js").status_code == 200
    assert client.get("/dashboard-assets/styles.css").status_code == 200
    assert client.get("/dashboard-assets/.env").status_code == 404
    assert client.get("/tools").status_code == 401


def test_dashboard_data_stays_authenticated_and_uncached(db, db_engine):
    client = TestClient(create_app(Settings(api_key=KEY, timezone="UTC"), db_engine))
    headers = {"Authorization": "Bearer " + KEY}
    response = client.post(
        "/tools/events",
        headers=headers,
        json={"arguments": {"start": "2026-09-10T00:00:00Z", "end": "2026-09-11T00:00:00Z"}},
    )
    assert response.status_code == 200 and response.json()["rows"] == []
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/tools", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_authenticated_dashboard_export_preserves_contract(db, db_engine):
    client = TestClient(create_app(Settings(api_key=KEY, timezone="UTC"), db_engine))
    params = {
        "start": "2026-09-10T00:00:00Z",
        "end": "2026-09-11T00:00:00Z",
        "timezone": "UTC",
        "format": "json",
    }
    assert client.get("/exports/diary", params=params).status_code == 401
    response = client.get(
        "/exports/diary", params=params, headers={"Authorization": "Bearer " + KEY}
    )
    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.headers["cache-control"] == "no-store"


def test_dashboard_filters_cover_every_supported_diary_kind(db_engine):
    from html.parser import HTMLParser

    from garmin_ai.events import EventInput

    values = set()

    class Options(HTMLParser):
        def handle_starttag(self, tag, attrs):
            value = dict(attrs).get("value")
            if tag == "option" and value:
                values.add(value)

    client = TestClient(create_app(Settings(api_key=KEY), db_engine))
    Options().feed(client.get("/dashboard").text)
    assert values == set(
        EventInput.model_json_schema()["properties"]["payload"]["discriminator"]["mapping"]
    )
