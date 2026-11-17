"""Compare native Garmin health tool requests with the pinned Python service."""

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.models import HealthDay, SourcePayload

TOKENS = {
    "health": "synthetic-health-http-token-" + "a" * 20,
    "diary": "synthetic-diary-http-token-" + "a" * 20,
    "invalid": "synthetic-unregistered-http-token-" + "a" * 20,
}
SCOPED = [
    {"key": TOKENS["health"], "scopes": ["read:health"]},
    {"key": TOKENS["diary"], "scopes": ["read:diary"]},
]
TOOLS = {"health_snapshot", "health_range"}
SNAPSHOT = "/tools/health_snapshot"
RANGE = "/tools/health_range"


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


def compare(python, native, cases):
    count = 0
    for method, route, credential, body in cases:
        headers = {"Authorization": "Bearer " + TOKENS[credential]} if credential else {}
        payload = None if body is None else json.dumps(body).encode()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        expected = python.request(method, route, headers=headers, content=payload)
        actual = native.request(method, route, headers=headers, content=payload)
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
        settings = Settings(api_tokens=SCOPED, api_key="")
        with TestClient(create_app(settings, engine=engine)) as python:
            with subprocess.Popen(
                [binary, "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                    "GA_API_TOKENS": json.dumps(SCOPED),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as process:
                try:
                    line = process.stdout.readline().strip()
                    assert line.startswith("Listening on http://127.0.0.1:"), line
                    with httpx.Client(base_url=line.split(" on ", 1)[1], trust_env=False) as native:
                        count = compare(python, native, initial_cases())
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
