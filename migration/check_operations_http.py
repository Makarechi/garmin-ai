"""Compare the owner-only operations and metrics endpoints with the Python baseline."""

import argparse
import json
import math
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import Settings
from garmin_ai.models import AppState, Job, SourcePayload

TOKENS = [
    {"key": "synthetic-operations-http-owner-token", "scopes": ["admin"]},
    {"key": "synthetic-operations-http-reader-token", "scopes": ["read:diary", "read:health"]},
]
ADMIN, READER = [{"Authorization": f"Bearer {token['key']}"} for token in TOKENS]
RESTRICTED = 'synthetic_restricted_"label\nfragment'
CAPACITY_KEY = "storage:backup-capacity"
CONNECTION_KEY = "integration:garmin"


def write_marker(engine, key, value):
    with Session(engine) as session, session.begin():
        session.merge(AppState(key=key, value=value))


def seed(engine):
    now = datetime.now(UTC)
    capacity = {
        "at": (now - timedelta(minutes=1)).isoformat(),
        "status": "insufficient",
        "volumes": [
            {"role": "staging", "free_bytes": 100, "required_bytes": 200, "path": RESTRICTED},
            {"role": "destination", "free_bytes": 300, "required_bytes": 250, "path": RESTRICTED},
        ],
        "private": RESTRICTED,
    }
    with Session(engine) as session, session.begin():
        for key, value in {
            CAPACITY_KEY: capacity,
            CONNECTION_KEY: {
                "status": "rate_limited",
                "blocked_until": (now + timedelta(minutes=5)).isoformat(),
                "private": RESTRICTED,
            },
            "runtime:heartbeat": {
                "at": (now - timedelta(seconds=20)).isoformat(),
                "id": RESTRICTED,
            },
            "backup:last_success": {
                "at": (now - timedelta(hours=2)).isoformat(),
                "file": RESTRICTED,
            },
        }.items():
            session.merge(AppState(key=key, value=value))
        jobs = [
            ("garmin_endpoint", "pending", -300),
            ("garmin_fit", "pending", -240),
            ("backup", "pending", -30),
            ("telegram_update", "pending", -15),
            ("telegram_ack", "done", -60),
            ("source_plugin_poll", "running", -25),
            ("agent_insights", "pending", 300),
            (RESTRICTED, "pending", -60),
            ("storage_check", RESTRICTED, -60),
        ]
        for index, (kind, status, offset) in enumerate(jobs):
            session.add(
                Job(
                    kind=kind,
                    status=status,
                    run_at=now + timedelta(seconds=offset),
                    dedup_key=f"synthetic-operations-{index}",
                    payload={"private": RESTRICTED},
                )
            )
        sources = [
            ("daily", "normalized"),
            ("daily", "normalized"),
            ("activity_fit", "archived"),
            ("source-record-v1", "raw_only"),
            (RESTRICTED, RESTRICTED),
        ]
        for index, (endpoint, status) in enumerate(sources):
            session.add(
                SourcePayload(
                    endpoint=endpoint,
                    status=status,
                    source_key=f"synthetic-{index}",
                    payload_hash="a" * 64,
                    archive_key=RESTRICTED,
                    payload={"private": RESTRICTED},
                )
            )
    return capacity


def report(client, route, expected_type):
    response = client.get(route, headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == expected_type
    assert "synthetic_restricted" not in response.text
    return response


def operations(client):
    document = report(client, "/operations", "application/json").json()
    return document


def compare_operations(reference, candidate):
    age_fields = ("runtime_heartbeat_age_seconds", "backup_age_seconds")
    for field in age_fields:
        assert math.isclose(reference.pop(field), candidate.pop(field), abs_tol=2)
    for key, sort_by in {
        "queue_due": ("lane",),
        "jobs": ("kind", "status"),
        "sources": ("endpoint", "status"),
    }.items():
        for value in (reference, candidate):
            value[key].sort(key=lambda row: tuple(row[field] for field in sort_by))
    for source, actual in zip(reference["queue_due"], candidate["queue_due"], strict=True):
        field = "oldest_due_age_seconds"
        assert math.isclose(source.pop(field), actual.pop(field), abs_tol=2)
    if reference["backup_capacity"]["available"]:
        assert math.isclose(
            reference["backup_capacity"].pop("age_seconds"),
            candidate["backup_capacity"].pop("age_seconds"),
            abs_tol=2,
        )
    assert candidate == reference, (reference, candidate)


def metrics(client):
    response = report(client, "/metrics", "text/plain; charset=utf-8")
    assert response.text.endswith("\n")
    return {
        key: float(value)
        for key, value in (line.rsplit(" ", 1) for line in response.text.splitlines())
    }


def exercise(engine, python, native):
    assert operations(native) == operations(python)
    initial = metrics(native)
    assert initial == metrics(python)
    assert initial["garmin_ai_runtime_heartbeat_age_seconds"] == -1
    assert initial["garmin_ai_backup_age_seconds"] == -1
    capacity = seed(engine)
    for route in ("/operations", "/metrics"):
        for headers, status in (({}, 401), (READER, 403)):
            expected = python.get(route, headers=headers)
            result = native.get(route, headers=headers)
            assert expected.status_code == result.status_code == status
            assert expected.json() == result.json()

    reference = operations(python)
    assert reference["garmin_connection"] == {"state": "rate_limited", "paused": True}
    assert reference["backup_capacity"]["sufficient"] is False
    compare_operations(reference, operations(native))
    source_metrics, native_metrics = metrics(python), metrics(native)
    assert source_metrics.keys() == native_metrics.keys()
    assert all(
        math.isclose(value, native_metrics[key], abs_tol=2 if "_age_seconds" in key else 0)
        for key, value in source_metrics.items()
    )

    reference = operations(python)
    with Session(engine) as session, session.begin():
        session.execute(
            update(Job).where(Job.kind == "telegram_update").values(kind="telegram_native_inbound")
        )
        session.execute(
            update(Job).where(Job.kind == "telegram_ack").values(kind="telegram_native_ack")
        )
    compare_operations(reference, operations(native))
    with Session(engine) as session, session.begin():
        session.execute(
            update(Job).where(Job.kind == "telegram_native_inbound").values(kind="telegram_update")
        )
        session.execute(
            update(Job).where(Job.kind == "telegram_native_ack").values(kind="telegram_ack")
        )

    for invalid in (
        {"at": "bad"},
        {"at": "2026-09-10T00:00:00"},
        {"at": "9999-01-01T00:00:00Z"},
        {"status": RESTRICTED},
        {"volumes": []},
        {"volumes": [None]},
        {"volumes": [{"role": RESTRICTED, "free_bytes": 1, "required_bytes": 2}]},
        {"volumes": [{"role": "shared", "free_bytes": True, "required_bytes": 2}]},
        {"volumes": [{"role": "shared", "free_bytes": 1, "required_bytes": RESTRICTED}]},
        {"volumes": [{"role": "shared", "free_bytes": 100, "required_bytes": 1}]},
    ):
        write_marker(engine, CAPACITY_KEY, capacity | invalid)
        expected = operations(python)["backup_capacity"]
        assert expected == {
            "available": False,
            "age_seconds": None,
            "sufficient": None,
            "volumes": [],
        }
        assert operations(native)["backup_capacity"] == expected
        assert metrics(native)["garmin_ai_backup_capacity_available"] == 0
    write_marker(engine, CONNECTION_KEY, {"status": "reauth_required", "blocked_until": "bad"})
    compare_operations(operations(python), operations(native))
    print(
        "Matched operations and Prometheus metrics, bounded labels, scopes and malformed metadata"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        settings = Settings(_env_file=None, api_tokens=TOKENS)
        with (
            TestClient(create_app(settings, engine)) as python,
            TemporaryDirectory(prefix="synthetic-operations-") as directory,
            subprocess.Popen(
                [args.binary.resolve(strict=True), "serve", "--port", "0"],
                cwd=directory,
                env={
                    "GA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
                    "GA_API_TOKENS": json.dumps(TOKENS),
                },
                stdout=subprocess.PIPE,
                text=True,
            ) as server,
        ):
            try:
                url = server.stdout.readline().strip().removeprefix("Listening on ")
                assert url.startswith("http://127.0.0.1:")
                with httpx.Client(base_url=url, timeout=6, trust_env=False) as native:
                    exercise(engine, python, native)
            finally:
                server.terminate()
                server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
