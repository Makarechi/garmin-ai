"""Verify that native calendar imports preserve the Python plan and revision contract."""

import argparse
import json
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text

from garmin_ai.api import create_app
from garmin_ai.config import Settings

TOKENS = [
    {"key": "synthetic-calendar-http-owner-token", "scopes": ["admin"]},
    {"key": "synthetic-calendar-http-diary-token", "scopes": ["read:diary", "write:diary"]},
]
ADMIN, READER = [{"Authorization": f"Bearer {token['key']}"} for token in TOKENS]
ROUTE = "/context/calendar"
SOURCE = str(uuid4())
FUTURE = str(uuid4())
NOW = datetime(2026, 9, 19, 10, tzinfo=UTC)
GRANT = {"id": SOURCE, "categories": ["work", "exercise"], "granted_at": "2026-01-01T00:00:00Z"}
FUTURE_GRANT = {"id": FUTURE, "categories": ["work"], "granted_at": "2099-01-01T00:00:00Z"}


def item(**changes):
    return {
        "source_id": SOURCE,
        "id": str(uuid4()),
        "revision": 1,
        "status": "busy",
        "start": NOW.isoformat(),
        "end": (NOW + timedelta(hours=1)).isoformat(),
        "timezone": "UTC",
        "category": "work",
    } | changes


def submit(client, items, status=200, access=ADMIN):
    response = client.post(ROUTE + "/import", json={"items": items}, headers=access)
    assert response.status_code == status, (response.status_code, response.text)
    return response.json()


def report(client, start=NOW, end=NOW + timedelta(days=1), status=200, access=ADMIN):
    response = client.get(
        ROUTE, params={"start": start.isoformat(), "end": end.isoformat()}, headers=access
    )
    assert response.status_code == status, (response.status_code, response.text)
    return response.json()


def count(engine):
    with engine.connect() as connection:
        return connection.scalar(
            text("SELECT count(*) FROM app_state WHERE starts_with(key,'calendar-context:')")
        )


def outcomes(document):
    return [record["status"] for record in document["outcomes"]]


def exercise(engine, python, native):
    original = item()
    for access, status in (({}, 401), (READER, 403)):
        assert submit(native, [original], status, access) == submit(
            python, [original], status, access
        )
        assert report(native, status=status, access=access) == report(
            python, status=status, access=access
        )
    assert count(engine) == 0
    assert report(native) == report(python)
    for reason in ({"source_id": FUTURE}, {"category": "personal"}):
        rejected = item(**reason)
        for client in (python, native):
            assert submit(client, [original, rejected], 422) == {"detail": "Invalid arguments"}
        assert count(engine) == 0
    for field in ("title", "attendees", "description", "location", "url"):
        submit(python, [original | {field: "synthetic_rejected_text"}], 422)
        denied = submit(native, [original | {field: "synthetic_rejected_text"}], 422)
        assert "synthetic_rejected_text" not in json.dumps(denied)
    assert count(engine) == 0

    assert outcomes(submit(python, [original])) == ["busy"]
    assert outcomes(submit(native, [original])) == ["unchanged"]
    moved = item(
        start="2026-09-19T19:00:00+09:00",
        end="2026-09-19T20:00:00+09:00",
        timezone="Asia/Tokyo",
        category="exercise",
    )
    assert outcomes(submit(native, [moved])) == ["busy"]
    assert outcomes(submit(python, [moved])) == ["unchanged"]
    rows = report(native)
    assert rows == report(python)
    assert rows["evidence_type"] == "calendar_plan" and len(rows["rows"]) == 2
    assert all(row["start"] == NOW.isoformat() for row in rows["rows"])
    assert not report(native, NOW + timedelta(hours=1))["rows"]

    revised = original | {"revision": 2, "end": (NOW + timedelta(hours=2)).isoformat()}
    assert outcomes(submit(native, [revised])) == ["busy"]
    assert outcomes(submit(python, [original])) == ["stale"]
    changed = original | {"end": (NOW + timedelta(hours=3)).isoformat()}
    assert submit(python, [changed], 409) == submit(native, [changed], 409)
    cancelled = {"source_id": SOURCE, "id": original["id"], "revision": 3, "status": "cancelled"}
    assert outcomes(submit(native, [cancelled])) == ["cancelled"]
    assert outcomes(submit(python, [cancelled])) == ["unchanged"]
    assert outcomes(submit(native, [revised])) == ["stale"]
    remaining = report(native)
    assert remaining == report(python)
    assert [row["id"] for row in remaining["rows"]] == [moved["id"]]
    plan_key = f"calendar-context:{SOURCE}:{original['id']}"
    with engine.connect() as connection:
        saved = connection.scalar(
            text("SELECT value FROM app_state WHERE key=:key"), {"key": plan_key}
        )
        assert [past["revision"] for past in saved["previous_revisions"]] == [1, 2]
        assert connection.scalar(text("SELECT count(*) FROM events")) == 0
        assert connection.scalar(text("SELECT count(*) FROM timeline_intervals")) == 0
    return moved


@contextmanager
def serving(binary, engine, sources):
    with (
        TemporaryDirectory(prefix="synthetic-calendar-http-") as directory,
        subprocess.Popen(
            [binary, "serve", "--port", "0"],
            cwd=directory,
            env={
                "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                "GA_API_TOKENS": json.dumps(TOKENS),
                "GA_CALENDAR_SOURCES": json.dumps(sources),
            },
            stdout=subprocess.PIPE,
            text=True,
        ) as server,
    ):
        try:
            url = server.stdout.readline().strip().removeprefix("Listening on ")
            assert url.startswith("http://127.0.0.1:")
            with httpx.Client(base_url=url, timeout=6, trust_env=False) as client:
                yield client
        finally:
            server.terminate()
            server.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    engine = isolated_engine()
    settings = Settings(_env_file=None, api_tokens=TOKENS, calendar_sources=[GRANT, FUTURE_GRANT])
    try:
        with (
            TestClient(create_app(settings, engine)) as python,
            serving(binary, engine, [GRANT, FUTURE_GRANT]) as native,
        ):
            moved = exercise(engine, python, native)
            for sources, availability in (
                ([], "disabled"),
                ([GRANT | {"categories": ["work"]}], "enabled"),
            ):
                with serving(binary, engine, sources) as revoked:
                    observed = report(revoked)
                    assert observed["availability"] == availability and observed["rows"] == []
                    submit(revoked, [moved], 422)
        versions = [GRANT, GRANT | {"granted_at": FUTURE_GRANT["granted_at"]}]
        for sources in (versions, versions[::-1]):
            with serving(binary, engine, sources) as invalid:
                assert report(invalid, status=422) == {"detail": "Invalid arguments"}
    finally:
        engine.dispose()
    print("Matched calendar plans, revision hashes, opt-in roles, revocation and atomic rollback")


if __name__ == "__main__":
    main()
