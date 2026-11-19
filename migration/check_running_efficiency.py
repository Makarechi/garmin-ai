"""Compare bounded running analysis and its temporal evidence with pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from capture_reference import isolated_engine
from check_temporal_displays import configure_zone
from check_temporal_features import EVENT, SOURCE, seed
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


def transition_cases(engine, compare):
    observations = [
        ("training_readiness_score", "2026-11-01T05:45:00Z", 45),
        ("training_readiness_score", "2026-11-01T06:15:00Z", 15),
        ("sleep_score", "2026-11-01T05:45:00Z", 65),
        ("hrv_nightly_avg", "2026-10-31T05:45:00Z", 35),
        ("hrv_nightly_avg", "2026-03-07T08:00:00Z", 38),
        ("sleep_score", "2026-03-08T07:15:00Z", 78),
        ("training_readiness_score", "2026-10-25T00:45:00Z", 25),
        ("training_readiness_score", "2026-10-25T01:15:00Z", 26),
        ("training_readiness_score", "2026-04-04T14:55:00Z", 44),
        ("training_readiness_score", "2026-04-04T15:10:00Z", 45),
    ]
    clocks = [
        "2026-11-01T05:20:00Z",
        "2026-11-01T05:30:00Z",
        "2026-11-01T06:20:00Z",
        "2026-11-01T06:30:00Z",
        "2026-11-02T06:20:00Z",
        "2026-03-08T07:30:00Z",
        "2026-03-09T06:30:00Z",
        "2026-10-25T01:20:00Z",
        "2026-04-04T15:20:00Z",
    ]
    clocks = [datetime.fromisoformat(clock) for clock in clocks]
    with engine.begin() as connection:
        for index, (metric, observed, value) in enumerate(observations):
            at = datetime.fromisoformat(observed)
            connection.execute(
                text("""INSERT INTO metric_observations(id,source_ref,metric,unit,value,
                source_calendar_date,observed_at,fetched_at,ingested_at,timezone,quality,sequence,feature_version,valid)
                VALUES(gen_random_uuid(),:source,:metric,'synthetic',:value,:day,:at,:known,:known,'UTC','observed',:sequence,'pre-event-v1',true)"""),
                {
                    "source": SOURCE,
                    "metric": metric,
                    "value": value,
                    "day": at.date(),
                    "at": at,
                    "known": at - timedelta(days=1),
                    "sequence": 1000000 + index,
                },
            )
        for index, at in enumerate(clocks):
            connection.execute(
                text("""INSERT INTO activities(id,kind,start,"end",timezone,
                distance_m,moving_seconds,duration_seconds,avg_hr,details)
                VALUES(:id,'running',:at,:at,'UTC',5000,1500,1500,135,'{}'::jsonb)"""),
                {"id": f"synthetic-clock-{index}", "at": at},
            )
    count = 0
    for zone in [
        "UTC",
        "America/New_York",
        "Europe/Bratislava",
        "Australia/Lord_Howe",
        "GMT+3",
        "GMT-3",
        "<+0545>-5:45",
        "EST5EDT",
        "Etc/GMT+3",
    ]:
        configure_zone(engine, zone)
        for at in clocks:
            result = compare(
                {
                    "start": (at - timedelta(microseconds=1)).isoformat(),
                    "end": (at + timedelta(microseconds=1)).isoformat(),
                }
            )
            assert result["n"] == 1
            count += 1
        result = compare({"start": "2026-11-01T05:00:00Z", "end": "2026-11-01T07:00:00Z"})
        assert result["n"] == 4
        count += 1
    configure_zone(engine, "America/New_York")
    configure_zone(engine, "GMT+3", role=True)
    for at in clocks:
        result = compare(
            {
                "start": (at - timedelta(microseconds=1)).isoformat(),
                "end": (at + timedelta(microseconds=1)).isoformat(),
            }
        )
        assert result["n"] == 1
        count += 1
    result = compare({"start": "2026-11-01T05:00:00Z", "end": "2026-11-01T07:00:00Z"})
    assert result["n"] == 4
    count += 1
    configure_zone(engine, "UTC", role=True)
    configure_zone(engine, "UTC")
    print("Matched running regional-clock and DST batches", count, flush=True)


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
            assert original.json() == candidate.json()
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
    first_day = {"start": "0001-01-01T00:00:00Z", "end": "0001-01-02T00:00:00Z"}
    compare(first_day, 422)
    compare({"start": "9999-12-31T00:00:00Z", "end": "9999-12-31T23:59:59Z"}, 422)
    for zone in ["America/New_York", "Asia/Kathmandu", "UTC"]:
        configure_zone(engine, zone)
        assert compare(request)["n"] == 4
    transition_cases(engine, compare)
    early = EVENT - timedelta(days=2)
    observed = early - timedelta(hours=1)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO activities(id,kind,start,\"end\",timezone,details) VALUES ('old-unqualified','running',:start,:start,'UTC','{}'::jsonb)"
            ),
            {"start": early},
        )
        connection.execute(
            text("""INSERT INTO metric_observations(id,source_ref,metric,unit,value,
            source_calendar_date,observed_at,fetched_at,ingested_at,timezone,quality,sequence,feature_version,valid)
            SELECT gen_random_uuid(),:source,'training_readiness_score','synthetic',1,DATE '2026-10-23',
                :observed,:known,:known,'UTC','observed',index+1000,'pre-event-v1',true FROM generate_series(1,100001) AS items(index)"""),
            {"source": SOURCE, "observed": observed, "known": EVENT},
        )
    compare({**request, "start": (EVENT - timedelta(days=3)).isoformat()}, 422)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM metric_observations WHERE source_ref=:source AND metric='training_readiness_score' AND observed_at=:observed"
            ),
            {"source": SOURCE, "observed": observed},
        )
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO activities(id,kind,start,"end",timezone,distance_m,moving_seconds,duration_seconds,avg_hr,details)
            SELECT concat('same-time:',i),'running',:at,:at,'UTC',5000,1500,1800,135,'{}'::jsonb
            FROM generate_series(1,2) i
        """),
            {"at": EVENT + timedelta(microseconds=120000)},
        )
    assert compare(request)["n"] == 6
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
            TestClient(create_app(settings, engine), raise_server_exceptions=False) as python,
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
