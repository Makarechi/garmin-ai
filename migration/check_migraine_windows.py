"""Compare migraine controls, coverage and seeded estimates with pinned Python."""

import argparse
import json
import math
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import ensure_system_definition
from garmin_ai.models import Event, HealthDay

BOTH = "synthetic-migraine-both-reader-validation-only"
HEALTH = "synthetic-migraine-health-reader-validation-only"
DIARY = "synthetic-migraine-diary-reader-validation-only"
TOKENS = [
    {"key": BOTH, "scopes": ["read:health", "read:diary"]},
    {"key": HEALTH, "scopes": ["read:health"]},
    {"key": DIARY, "scopes": ["read:diary"]},
]
PATH = "/tools/analysis_migraine_windows"
BASE = date(2024, 1, 1)
MAXIMUM_BASE = date(2060, 1, 1)
LAST_EPISODE = UUID(int=9950)


def seed_migraine(engine, base=BASE):
    with Session(engine) as session, session.begin():
        versions = {
            kind: ensure_system_definition(session, kind)
            for kind in ("migraine", "headache_observation", "symptom_observation")
        }

        def event(kind, start, end=None, payload=None, **flags):
            row = Event(
                definition_version_id=versions[kind].id,
                topology=versions[kind].topology,
                kind=kind,
                start=start,
                end=end,
                timezone="UTC",
                source=flags.pop("source", "manual"),
                status=flags.pop("status", "confirmed"),
                payload={"type": kind} | (payload or {}),
                **flags,
            )
            session.add(row)
            return row

        for index in range(-56, 1057):
            day = base + timedelta(days=index)
            session.add(
                HealthDay(
                    day=day,
                    sleep_score=None if index % 19 == 0 else (index * 13) % 101,
                    resting_hr=60 + math.sin(index) * 10,
                    sources={},
                )
            )
            left = datetime.combine(day, datetime.min.time(), UTC)
            right = left + timedelta(days=1)
            mode = index % 10
            if mode == 0:
                continue
            payload = {"headache": "no", "migraine": "no"}
            flags = {}
            if mode == 1:
                payload["headache"] = "unknown"
            if mode == 7:
                flags["status"] = "needs_confirmation"
            if mode == 8:
                flags.update(source="inferred", status="inferred")
            middle = left + timedelta(hours=12)
            if mode == 2:
                event("headache_observation", left, middle, payload)
            elif mode == 3:
                event("headache_observation", left, middle, payload)
                event("headache_observation", middle, right, payload)
            else:
                event("headache_observation", left, right, payload, **flags)
            if mode in (4, 5, 6):
                symptom = {"episode_id": str(UUID(int=1)), "severity": 0}
                if mode == 4:
                    symptom["severity"] = 2
                if mode == 5:
                    symptom["impact"] = "Synthetic interference"
                event("symptom_observation", middle, payload=symptom)

        for index in range(32):
            left = datetime.combine(base + timedelta(days=index * 28), datetime.min.time(), UTC)
            event(
                "migraine",
                left + timedelta(hours=12),
                left + timedelta(hours=15),
                **({"id": UUID(int=1)} if index == 0 else {}),
            )
        left = datetime.combine(base, datetime.min.time(), UTC)
        event("migraine", left + timedelta(hours=13), left + timedelta(hours=14))
        event("migraine", left + timedelta(days=2), deleted=True)
        event("migraine", left + timedelta(days=3), status="needs_confirmation")
        # A very old open episode must remain visible to the overlap query.
        event("migraine", left - timedelta(days=900), id=UUID(int=9900), deleted=True)


def seed_maximum(engine):
    with Session(engine) as session, session.begin():
        migraine = ensure_system_definition(session, "migraine")
        negative = ensure_system_definition(session, "headache_observation")
        for index in range(-56, 3717):
            session.add(
                HealthDay(
                    day=MAXIMUM_BASE + timedelta(days=index),
                    sleep_score=(index * 13) % 101,
                    sources={},
                )
            )
        session.add(
            Event(
                definition_version_id=negative.id,
                topology=negative.topology,
                kind="headache_observation",
                timezone="UTC",
                source="manual",
                status="confirmed",
                start=datetime.combine(MAXIMUM_BASE - timedelta(days=56), datetime.min.time(), UTC),
                end=datetime.combine(MAXIMUM_BASE + timedelta(days=3717), datetime.min.time(), UTC),
                payload={"type": "headache_observation", "headache": "no", "migraine": "no"},
            )
        )
        for index in range(201):
            start = datetime.combine(
                MAXIMUM_BASE + timedelta(days=index * 14), datetime.min.time(), UTC
            )
            flags = {"id": LAST_EPISODE} if index == 200 else {}
            session.add(
                Event(
                    definition_version_id=migraine.id,
                    topology=migraine.topology,
                    kind="migraine",
                    timezone="UTC",
                    source="manual",
                    status="confirmed",
                    start=start + timedelta(hours=12),
                    end=start + timedelta(hours=15),
                    payload={"type": "migraine"},
                    **flags,
                )
            )


def differences(expected, actual, path=()):
    if isinstance(expected, dict) and isinstance(actual, dict):
        if expected.keys() != actual.keys():
            return path, sorted(expected), sorted(actual)
        for key in expected:
            mismatch = differences(expected[key], actual[key], path + (key,))
            if mismatch:
                return mismatch
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return path, len(expected), len(actual)
        for index, (first, second) in enumerate(zip(expected, actual, strict=True)):
            mismatch = differences(first, second, path + (index,))
            if mismatch:
                return mismatch
        return None
    if isinstance(expected, float) and isinstance(actual, (float, int)):
        if math.isclose(expected, actual, rel_tol=1e-12, abs_tol=1e-12):
            return None
    if expected != actual:
        return path, str(expected)[:200], str(actual)[:200]
    return None


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
            [(reply.status_code, reply.text[:1000]) for reply in replies],
        )
        if status != 422:
            expected, actual = [reply.json() for reply in replies]
            mismatch = differences(expected, actual)
            assert mismatch is None, (arguments, mismatch)
        return replies[1].json()

    request = {
        "metric": "sleep_score",
        "start": "2024-01-01",
        "end": "2026-09-27",
        "timezone": "UTC",
    }
    compare(request, 401, token=None)
    compare(request, 403, token=HEALTH)
    compare(request, 403, token=DIARY)
    result = compare(request)
    assert result["matched_pairs"] >= 10 and result["ci95"] is not None
    assert result["episodes"] > result["episode_start_days"]
    assert result["control_days_truncated"] and len(result["control_days"]) == 50
    assert set(result["control_coverage_counts"]) == {
        "positive",
        "unknown",
        "unanswered",
        "incomplete",
        "confirmed_negative",
    }
    for zone in ("Europe/Bratislava", "Asia/Kathmandu", "America/Havana", "Australia/Lord_Howe"):
        compare(request | {"timezone": zone})
    compare(request | {"metric": "resting_hr"})
    for timezone in (None, ""):
        compare(request | {"timezone": timezone})
    compare({key: value for key, value in request.items() if key != "timezone"})
    compare(request | {"start": "2024-01-01", "end": "2024-01-01"})
    compare(request | {"start": "2020-01-01", "end": "2020-01-01"})
    for invalid in (
        {"metric": "hrv_status"},
        {"metric": "missing"},
        {"timezone": "Invalid/Zone"},
        {"timezone": True},
        {"start": "0001-01-01", "end": "0001-01-01"},
        {"start": "9999-12-31", "end": "9999-12-31"},
        {"start": request["end"], "end": request["start"]},
    ):
        compare(request | invalid, 422)
    with Session(engine) as session, session.begin():
        session.execute(
            text("UPDATE events SET deleted=false WHERE id=:id"), {"id": UUID(int=9900)}
        )
    opened = compare(request)
    assert opened["matched_pairs"] == 0
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE events SET deleted=true WHERE id=:id"), {"id": UUID(int=9900)})
    compare(request)
    seed_maximum(engine)
    maximum = {
        "metric": "sleep_score",
        "start": str(MAXIMUM_BASE),
        "end": str(MAXIMUM_BASE + timedelta(days=199 * 14)),
        "timezone": "UTC",
    }
    accepted = compare(maximum)
    assert accepted["matched_pairs"] == 200 and accepted["pairs_truncated"]
    compare(maximum | {"end": str(MAXIMUM_BASE + timedelta(days=200 * 14))}, 422)
    with Session(engine) as session, session.begin():
        session.execute(text("UPDATE events SET deleted=true WHERE id=:id"), {"id": LAST_EPISODE})
    accepted = compare(maximum | {"end": str(MAXIMUM_BASE + timedelta(days=3660))})
    assert accepted["matched_pairs"] == 200 and accepted["control_days_total"] == 3773
    compare(maximum | {"end": str(MAXIMUM_BASE + timedelta(days=3661))}, 422)
    print(
        "Matched migraine controls, exact coverage/counts/matching, timezones, open episodes and 1e-12 seeded estimates"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed_migraine(engine)
        settings = Settings(
            _env_file=None, timezone="UTC", api_tokens=[ApiToken(**token) for token in TOKENS]
        )
        with (
            TestClient(create_app(settings, engine)) as python,
            subprocess.Popen(
                [args.binary.resolve(strict=True), "serve", "--port", "0"],
                env={
                    "GA_DATABASE_URL": os.environ["GA_TEST_DATABASE_URL"],
                    "GA_API_TOKENS": json.dumps(TOKENS),
                    "GA_TIMEZONE": "UTC",
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as server,
        ):
            try:
                url = server.stdout.readline().strip().removeprefix("Listening on ")
                assert url.startswith("http://127.0.0.1:")
                with httpx.Client(base_url=url, timeout=120, trust_env=False) as native:
                    exercise(engine, python, native)
            finally:
                server.terminate()
                server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
