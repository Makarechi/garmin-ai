"""Compare event-relative measured windows with the pinned Python application."""

import argparse
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import httpx
from capture_reference import isolated_engine
from check_metric_series import AT, seed
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import ensure_system_definition
from garmin_ai.models import Event

BOTH = "synthetic-event-window-reader-validation-only"
HEALTH = "synthetic-event-health-reader-validation-only"
DIARY = "synthetic-event-diary-reader-validation-only"
TOKENS = [
    {"key": BOTH, "scopes": ["read:health", "read:diary"]},
    {"key": HEALTH, "scopes": ["read:health"]},
    {"key": DIARY, "scopes": ["read:diary"]},
]
PATH = "/tools/analysis_event_windows"


def seed_events(engine):
    seed(engine)
    with Session(engine) as session, session.begin():
        for index, (offset, kind, status, deleted) in enumerate(
            [
                (timedelta(), "note", "confirmed", False),
                (timedelta(minutes=7, seconds=11, microseconds=123456), "note", "confirmed", False),
                (timedelta(days=1), "note", "needs_confirmation", False),
                (timedelta(days=2), "note", "confirmed", True),
                (timedelta(days=3), "migraine", "confirmed", False),
                (timedelta(days=4), "note", "inferred", False),
                (timedelta(days=5), "note", "confirmed", False),
            ]
        ):
            version = ensure_system_definition(session, kind)
            session.add(
                Event(
                    definition_version_id=version.id,
                    topology=version.topology,
                    id=UUID(int=200 + index),
                    kind=kind,
                    start=AT + offset,
                    timezone="UTC",
                    source="manual",
                    status=status,
                    deleted=deleted,
                    payload={"type": "migraine", "severity": 3}
                    if kind == "migraine"
                    else {"type": kind, "description": "Synthetic episode"},
                )
            )


def exercise(engine, python, native):
    def compare(arguments, status=200, token=BOTH):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        replies = [
            client.post(PATH, json={"arguments": arguments}, headers=headers)
            for client in (python, native)
        ]
        assert all(reply.status_code == status for reply in replies), (
            arguments,
            status,
            [(reply.status_code, reply.text[:2000]) for reply in replies],
        )
        if status != 422:
            assert replies[0].json() == replies[1].json(), (
                arguments,
                replies[0].json(),
                replies[1].json(),
            )
        return replies[1].json()

    request = {
        "event_type": "note",
        "metric": "heart_rate_bpm",
        "start": AT.isoformat(),
        "end": (AT + timedelta(days=5)).isoformat(),
    }
    compare(request, 401, token=None)
    compare(request, 403, token=HEALTH)
    compare(request, 403, token=DIARY)
    result = compare(request)
    assert result["episodes"] == 2
    assert [row["event_id"] for row in result["rows"]] == [str(UUID(int=200)), str(UUID(int=201))]
    assert all(len(row["windows"]) == 5 for row in result["rows"])
    assert result["rows"][0]["windows"][0]["sources"] == []
    assert result["rows"][0]["windows"][-1]["sources"]
    compare(request | {"event_type": "system.note"})
    increments = compare(request | {"metric": "steps_bucket"})
    assert [row["sum"] for row in increments["rows"][0]["windows"][-1]["sources"]] == [60, 12]
    compare(request | {"event_type": "migraine"})
    compare(request | {"event_type": "missing"}, 422)
    compare(request | {"event_type": "user.synthetic"}, 422)
    compare(request | {"metric": "sleep_score"}, 422)
    compare(request | {"end": request["start"]}, 422)
    compare(request | {"end": (AT + timedelta(days=367)).isoformat()}, 422)
    compare(request | {"start": "2026-10-25T14:30:00+05:30"})
    empty = compare(request | {"event_type": "hydration"})
    assert empty["episodes"] == 0 and empty["rows"] == []
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO measurements(ts,metric,source,local_date,value,unit,quality,details)
            SELECT :at,'stress_score',concat('synthetic-source:',lpad(i::text,3,'0')),
            CAST(:at AS date),1,'score','observed','{}'::jsonb FROM generate_series(1,33) i"""),
            {"at": AT},
        )
    capped = compare(request | {"metric": "stress_score"})
    assert len(capped["rows"][0]["windows"][-1]["sources"]) == 32
    assert capped["rows"][0]["windows"][-1]["truncated"]
    future = AT + timedelta(days=100)
    with Session(engine) as session, session.begin():
        version = ensure_system_definition(session, "note")
        session.add_all(
            [
                Event(
                    definition_version_id=version.id,
                    topology=version.topology,
                    id=UUID(int=1000 + index),
                    kind="note",
                    start=future + timedelta(seconds=index),
                    timezone="UTC",
                    source="manual",
                    payload={"type": "note", "description": "Synthetic cap"},
                )
                for index in range(101)
            ]
        )
    bound = request | {"start": future.isoformat(), "end": (future + timedelta(days=1)).isoformat()}
    compare(bound, 422)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM events WHERE id=:id"), {"id": UUID(int=1100)})
    maximum = compare(bound)
    assert maximum["episodes"] == 100 and len(maximum["rows"]) == 100
    print(
        "Matched five event-relative windows, confirmed episodes, source caps, scopes and 100-episode boundary"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed_events(engine)
        settings = Settings(_env_file=None, api_tokens=[ApiToken(**token) for token in TOKENS])
        with (
            TestClient(create_app(settings, engine)) as python,
            subprocess.Popen(
                [args.binary.resolve(strict=True), "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                    "GA_API_TOKENS": json.dumps(TOKENS),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as server,
        ):
            try:
                url = server.stdout.readline().strip().removeprefix("Listening on ")
                assert url.startswith("http://127.0.0.1:")
                with httpx.Client(base_url=url, timeout=10, trust_env=False) as native:
                    exercise(engine, python, native)
            finally:
                server.terminate()
                server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
