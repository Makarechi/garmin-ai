"""Compare bounded running analysis and its temporal evidence with pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
from capture_reference import isolated_engine
from check_temporal_features import EVENT, seed
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.models import Activity

HEALTH = "synthetic-running-analysis-health-reader"
DIARY = "synthetic-running-analysis-diary-reader"
TOKENS = [{"key": HEALTH, "scopes": ["read:health"]}, {"key": DIARY, "scopes": ["read:diary"]}]
PATH = "/tools/analysis_running_efficiency"
START = EVENT.isoformat()
END = (EVENT + timedelta(days=1)).isoformat()


def seed_activities(engine):
    seed(engine)
    with Session(engine) as session, session.begin():
        cases = [
            ("road", "running", 1500, 2000, 5000, 135, 72),
            ("trail", "trail_running", None, 3000, 6000, 148, 150),
            ("treadmill", "treadmill_running", 0, 1200, 2200, 150, None),
            ("short", "track_running", 1199, 4500, 6000, 149, 0),
            ("other", "walking", 4500, 4500, 5000, 148, 0),
            ("no-distance", "virtual_run", 2600, 3000, None, 149, None),
            ("no-hr", "indoor_running", 3000, 3400, 4000, None, None),
            ("above", "obstacle_run", 1200, 1600, 2000, 205, None),
            ("at-end", "ultra_run", 6000, 6000, 6000, 149, None),
        ]
        for index, (identity, kind, moving, total, distance, hr, ascent) in enumerate(cases):
            at = EVENT + timedelta(hours=index, microseconds=120000)
            if identity == "at-end":
                at = EVENT + timedelta(days=1)
            session.add(
                Activity(
                    id=identity,
                    kind=kind,
                    start=at,
                    end=at + timedelta(hours=1),
                    timezone="Europe/Bratislava",
                    moving_seconds=moving,
                    duration_seconds=total,
                    distance_m=distance,
                    avg_hr=hr,
                    ascent_m=ascent,
                )
            )


def normalized(document):
    for row in document.get("rows", []):
        for feature in row["context"].values():
            feature.pop("knowledge_cutoff")
    return document


def exercise(engine, python, rust):
    def compare(body, status=200, token=HEALTH):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        original, candidate = [
            client.post(PATH, json={"arguments": body}, headers=headers)
            for client in (python, rust)
        ]
        assert original.status_code == candidate.status_code == status, (
            body,
            original.text,
            candidate.text,
        )
        if status == 422:
            return None
        expected, actual = normalized(original.json()), normalized(candidate.json())
        assert expected == actual, (body, expected, actual)
        return actual

    request = {"start": START, "end": END}
    compare(request, 401, token=None)
    compare(request, 403, token=DIARY)
    recent = compare(request)
    assert recent["n"] == 4 and recent["excluded"] == 3
    assert [row["activity_id"] for row in recent["rows"]] == ["road", "trail", "treadmill", "above"]
    assert recent["rows"][0]["readiness"] == 90
    assert recent["mixed_activity_types"] and all(
        not row["physiologically_comparable"] for row in recent["rows"]
    )
    assert compare(request | {"hr_min": 148, "hr_max": 150})["n"] == 2
    assert compare(request | {"hr_min": 251, "hr_max": 300})["n"] == 0
    compare(request | {"hr_min": 200, "hr_max": 100}, 422)
    compare(request | {"end": START}, 422)
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO activities(id,kind,start,"end",timezone,details)
            SELECT concat('limit:',i),'running',:start,:start,'UTC','{}'::jsonb FROM generate_series(1,2001) i
        """),
            {"start": EVENT},
        )
    compare(request, 422)
    print("Matched running cohorts, pre-run context, access, range rejection and row cap")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed_activities(engine)
        settings = Settings(_env_file=None, api_tokens=[ApiToken(**spec) for spec in TOKENS])
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
                with httpx.Client(base_url=url, timeout=5, trust_env=False) as rust:
                    exercise(engine, python, rust)
            finally:
                server.terminate()
                server.wait(timeout=5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
