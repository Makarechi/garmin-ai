"""Compare native Garmin health tool requests with the pinned Python service."""

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from capture_reference import isolated_engine
from check_activity_reads import seed as seed_activities
from check_device_history import seed as seed_devices
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState, HealthDay, SourcePayload, TimelineInterval

TOKENS = {
    "health": "synthetic-health-http-token-" + "a" * 20,
    "diary": "synthetic-diary-http-token-" + "a" * 20,
    "both": "synthetic-both-http-token-" + "a" * 20,
    "invalid": "synthetic-unregistered-http-token-" + "a" * 20,
}
SCOPED = [
    {"key": TOKENS["health"], "scopes": ["read:health"]},
    {"key": TOKENS["diary"], "scopes": ["read:diary"]},
    {"key": TOKENS["both"], "scopes": ["read:health", "read:diary"]},
]
TOOLS = {
    "health_snapshot",
    "health_range",
    "activities",
    "activity_details",
    "events",
    "timeline",
    "device_history",
    "data_freshness",
}
SNAPSHOT = "/tools/health_snapshot"
RANGE = "/tools/health_range"
ACTIVITIES = "/tools/activities"
DETAILS = "/tools/activity_details"
EVENTS = "/tools/events"
TIMELINE = "/tools/timeline"
DEVICES = "/tools/device_history"
FRESHNESS = "/tools/data_freshness"
SETUP_ZONE = "Pacific/Kiritimati"


def seed(engine):
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                HealthDay(
                    day=date(2026, 10, 24),
                    steps=9001,
                    resting_hr=54,
                    hydration_ml=0,
                    sources={"field:steps": "synthetic"},
                    updated_at=datetime(2026, 10, 25, 8, 0, 0, 421000, tzinfo=UTC),
                ),
                HealthDay(
                    day=date(2026, 10, 26),
                    training_status="ordinary",
                    sources={},
                    updated_at=datetime(2026, 10, 26, tzinfo=UTC),
                ),
            ]
        )


def seed_timeline(engine):
    start = datetime(2026, 10, 25, 9, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            TimelineInterval(
                id="synthetic-visit",
                label="synthetic planned activity",
                source="calendar",
                start=start,
                end=start + timedelta(minutes=55),
                confidence=0.8,
                confirmed=True,
                evidence={"fixture": "visit"},
            )
        )
        for begin, payload in [
            (start, {"type": "note", "description": "synthetic point"}),
            (start - timedelta(hours=1), {"type": "migraine"}),
            (start + timedelta(minutes=5), {"type": "medication"}),
        ]:
            create_event(
                session,
                EventInput(start=begin, payload=payload, original_text="synthetic input"),
                actor="fixture",
            )


def seed_freshness(engine):
    now = datetime.now(UTC)
    dates = {now.date(), now.astimezone(ZoneInfo(SETUP_ZONE)).date()}
    with Session(engine) as session, session.begin():
        for day in dates:
            session.add(
                AppState(
                    key=f"freshness:daily:{day}",
                    value={
                        "source_key": str(day),
                        "success_at": (now - timedelta(minutes=30)).isoformat(),
                        "fetched_at": (now - timedelta(seconds=20)).isoformat(),
                        "status": "normalized",
                    },
                )
            )


def initial_cases():
    yield ("GET", "/tools", "health", None)
    yield ("GET", "/tools", "diary", None)
    yield ("GET", "/tools", "invalid", None)
    yield ("GET", "/tools", None, None)
    yield ("POST", SNAPSHOT, "diary", {"arguments": {"day": "2026-10-24"}})
    yield ("POST", SNAPSHOT, "invalid", {"arguments": {"day": "2026-10-24"}})
    yield ("POST", "/tools/unassigned", "health", {})
    yield ("GET", SNAPSHOT, "health", None)
    yield ("HEAD", "/tools", "health", None)
    yield ("OPTIONS", SNAPSHOT, "health", None)
    for value in [
        "2026-10-24",
        "2026-10-25",
        "2026-10-24T00:00:00+02:00",
        "2026-10-24T00:00:00.000000001Z",
        "2026-10-23T22:00:00+02:00",
        1792800000,
        1792800000000,
        1792800000.0,
        "1792800000",
        True,
        "0001-01-01",
        "invalid",
    ]:
        yield ("POST", SNAPSHOT, "health", {"arguments": {"day": value}})
    yield ("POST", SNAPSHOT, "health", {"arguments": {"day": "2026-10-24", "unknown": False}})
    yield ("POST", SNAPSHOT, "health", {})
    yield ("POST", SNAPSHOT, "health", [])
    yield ("POST", SNAPSHOT, "health", None)
    yield ("POST", SNAPSHOT, "health", {"arguments": []})
    yield ("POST", SNAPSHOT, "health", {"unexpected": 1})
    yield ("POST", RANGE, "health", {"arguments": {"start": "2026-10-24", "end": "2026-10-26"}})
    yield ("POST", RANGE, "health", {"arguments": {"start": "2026-10-26", "end": "2026-10-24"}})
    yield ("POST", RANGE, "health", {"arguments": {"start": "0001-01-01", "end": "0001-01-02"}})


def activity_cases():
    first = "2026-10-25T11:00:00+02:00"
    last = "2026-10-25T12:00:00.000000001Z"
    period = {"start": first, "end": last}
    summary = {"activity_id": "run"}
    for token in ("diary", "invalid", None):
        yield ("POST", ACTIVITIES, token, {"arguments": period})
        yield ("POST", DETAILS, token, {"arguments": summary})
    for arguments in [
        period,
        {**period, "kind": None},
        {**period, "kind": "running"},
        {**period, "kind": ""},
        {**period, "kind": "absent"},
        {**period, "kind": False},
        {**period, "limit": 5},
        {**period, "start": "2026-10-25T09:00:00"},
        {**period, "start": "2026-10-25"},
        {**period, "start": "0001-01-01T00:00:00Z"},
        {"start": last, "end": first},
        {
            "start": datetime(2026, 10, 25, 9, tzinfo=UTC).timestamp(),
            "end": str(int(datetime(2026, 10, 25, 12, tzinfo=UTC).timestamp() * 1000)),
        },
        {},
    ]:
        yield ("POST", ACTIVITIES, "health", {"arguments": arguments})
    for arguments in [
        summary,
        {**summary, "include_samples": True, "limit": 4},
        {**summary, "include_samples": "YeS", "offset": "1_0", "limit": "+1.000"},
        {**summary, "include_samples": "0", "offset": -0.0, "limit": 2.0},
        {**summary, "include_samples": 1, "offset": True, "limit": "  1  "},
        {**summary, "include_samples": 2},
        {**summary, "include_samples": " yes "},
        {**summary, "offset": -1},
        {**summary, "offset": "1e0"},
        {**summary, "offset": 1_000_000},
        {**summary, "offset": 1_000_001},
        {**summary, "limit": 0},
        {**summary, "limit": 2001},
        {**summary, "limit": False},
        {**summary, "limit": "99.99"},
        {**summary, "limit": None},
        {**summary, "unknown": True},
        {"activity_id": "missing"},
        {"activity_id": "trainer"},
        {"activity_id": "bulky", "include_samples": True, "limit": 1},
        {"activity_id": 1},
        {},
    ]:
        yield ("POST", DETAILS, "health", {"arguments": arguments})


def diary_cases():
    first = "2026-10-25T11:00:00+02:00"
    last = "2026-10-25T06:00:00-04:00"
    period = {"start": first, "end": last}
    yield ("GET", "/tools", "both", None)
    for token in ("health", "invalid", None):
        yield ("POST", EVENTS, token, {"arguments": period})
    for token in ("health", "diary", "invalid", None):
        yield ("POST", TIMELINE, token, {"arguments": period})
    for arguments in [
        period,
        {**period, "kind": "migraine"},
        {**period, "kind": "system.medication"},
        {**period, "kind": ""},
        {**period, "kind": None},
        {**period, "kind": "user.unknown"},
        {**period, "kind": True},
        {**period, "start": last},
        {**period, "start": "2026-10-25T09:00:00"},
        {**period, "limit": 1},
    ]:
        yield ("POST", EVENTS, "diary", {"arguments": arguments})
    for arguments in [
        period,
        {**period, "end": "2026-10-25T09:01:00Z"},
        {**period, "end": "2026-10-25T11:00:00+02:00"},
        {**period, "end": "2026-10-25T09:00:00"},
        {**period, "end": "2026-11-26T00:00:00Z"},
        {**period, "kind": "note"},
        {},
        {
            "start": datetime(2026, 10, 25, 9, tzinfo=UTC).timestamp(),
            "end": datetime(2026, 10, 25, 10, tzinfo=UTC).timestamp(),
        },
    ]:
        yield ("POST", TIMELINE, "both", {"arguments": arguments})


def history_cases():
    period = {"start": "2026-10-25T11:00:00+02:00", "end": "2026-10-29T12:00:00Z"}
    for token in ("diary", "invalid", None):
        yield ("POST", DEVICES, token, {"arguments": period})
        yield ("POST", FRESHNESS, token, {})
    for arguments in [
        period,
        {**period, "limit": 1},
        {**period, "limit": "+2.000"},
        {**period, "limit": True},
        {**period, "limit": 0},
        {**period, "limit": 201},
        {**period, "limit": "2.5"},
        {**period, "limit": None},
        {**period, "end": "2026-10-25T11:00:00+02:00"},
        {**period, "end": "2026-10-25T10:30:00"},
        {**period, "end": "2027-11-01T00:00:00Z"},
        {**period, "kind": "running"},
        {},
        {"start": "2026-10-30T00:00:00Z", "end": "2026-10-31T00:00:00Z"},
    ]:
        yield ("POST", DEVICES, "health", {"arguments": arguments})
    yield ("POST", FRESHNESS, "health", {})
    yield ("POST", FRESHNESS, "both", {"arguments": {}})
    yield ("POST", FRESHNESS, "health", {"arguments": {"day": "2026-10-25"}})
    yield ("POST", FRESHNESS, "health", {"arguments": []})
    yield ("GET", FRESHNESS, "health", None)


def compare(python, native, cases):
    count = 0
    for method, route, credential, body in cases:
        headers = {"Authorization": "Bearer " + TOKENS[credential]} if credential else {}
        payload = None if body is None else json.dumps(body).encode()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        actual = native.request(method, route, headers=headers, content=payload)
        if route == FRESHNESS and actual.status_code == 200:
            at = datetime.fromisoformat(actual.json()["checked_at"])

            class RequestClock(datetime):
                @classmethod
                def now(cls, tz=None, *, instant=at):
                    return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

            with patch("garmin_ai.queries.datetime", RequestClock):
                expected = python.request(method, route, headers=headers, content=payload)
        else:
            expected = python.request(method, route, headers=headers, content=payload)
        desc = (method, route, credential, body)
        assert expected.status_code == actual.status_code, (desc, expected.status_code, actual.text)
        for name in ("cache-control", "allow", "retry-after"):
            assert expected.headers.get(name) == actual.headers.get(name), (
                desc,
                name,
                expected.headers.get(name),
                actual.headers.get(name),
            )
        if expected.content:
            outcome = expected.json()
            if route == "/tools" and expected.status_code == 200:
                outcome = [item for item in outcome if item["name"] in TOOLS]
            assert outcome == actual.json(), (desc, outcome, actual.json())
        else:
            assert actual.content == b"", desc
        count += 1
    return count


def run(binary):
    engine = isolated_engine()
    try:
        seed(engine)
        seed_activities(engine)
        seed_timeline(engine)
        seed_devices(engine)
        seed_freshness(engine)
        settings = Settings(api_tokens=SCOPED, api_key="", timezone="UTC")
        with TestClient(create_app(settings, engine=engine)) as python:
            with subprocess.Popen(
                [binary, "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                    "GA_API_TOKENS": json.dumps(SCOPED),
                    "GA_TIMEZONE": "UTC",
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as process:
                try:
                    line = process.stdout.readline().strip()
                    assert line.startswith("Listening on http://127.0.0.1:"), line
                    with httpx.Client(base_url=line.split(" on ", 1)[1], trust_env=False) as native:
                        count = compare(python, native, initial_cases())
                        count += compare(python, native, activity_cases())
                        count += compare(python, native, diary_cases())
                        count += compare(python, native, history_cases())
                        with Session(engine) as session, session.begin():
                            session.execute(
                                text("UPDATE people SET timezone=:zone"), {"zone": SETUP_ZONE}
                            )
                            session.add(AppState(key="preferences:onboarding", value={}))
                        count += compare(python, native, [("POST", FRESHNESS, "health", {})])
                        with Session(engine) as session, session.begin():
                            session.add(
                                SourcePayload(
                                    source="garmin_connect",
                                    endpoint="daily",
                                    source_key="2026-10-25",
                                    payload_hash="b" * 64,
                                    archive_key="sha256:" + "b" * 64,
                                    fetched_at=datetime(2026, 10, 25, tzinfo=UTC),
                                    parser_version=0,
                                    status="pending",
                                    payload={"totalSteps": 0},
                                )
                            )
                        count += compare(
                            python,
                            native,
                            [
                                ("POST", SNAPSHOT, "health", {"arguments": {"day": "2026-10-24"}}),
                                ("POST", SNAPSHOT, "health", {}),
                                ("POST", SNAPSHOT, "diary", {"arguments": {"day": "2026-10-24"}}),
                                ("POST", ACTIVITIES, "health", {}),
                                ("POST", DETAILS, "health", {"arguments": {"activity_id": "run"}}),
                                ("POST", DETAILS, "diary", {"arguments": {"activity_id": "run"}}),
                                ("POST", TIMELINE, "both", {}),
                                ("POST", TIMELINE, "diary", {}),
                                ("POST", DEVICES, "health", {}),
                                ("POST", DEVICES, "diary", {}),
                                ("POST", FRESHNESS, "health", {}),
                                (
                                    "POST",
                                    EVENTS,
                                    "diary",
                                    {
                                        "arguments": {
                                            "start": "2026-10-25T09:00:00Z",
                                            "end": "2026-10-25T10:00:00Z",
                                        }
                                    },
                                ),
                            ],
                        )
                        print(f"Matched {count} native health tool HTTP responses")
                finally:
                    process.terminate()
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
