"""Compare measured stress-hour coverage, daylight-saving boundaries and ranking with Python."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.models import Measurement

HEALTH = "synthetic-stress-health-reader-validation-only"
DIARY = "synthetic-stress-diary-reader-validation-only"
TOKENS = [{"key": HEALTH, "scopes": ["read:health"]}, {"key": DIARY, "scopes": ["read:diary"]}]
PATH = "/tools/analysis_stress_by_hour"


def seed_stress(engine):
    with Session(engine) as session, session.begin():

        def add(at, value, source="garmin_connect", unit="score", quality="observed"):
            session.add(
                Measurement(
                    ts=at,
                    local_date=at.date(),
                    metric="stress_score",
                    source=source,
                    unit=unit,
                    quality=quality,
                    value=value,
                )
            )

        for day in (1, 2, 3):
            for hour, value in ((9, 80), (16, 20)):
                at = datetime(2026, 9, day, hour, tzinfo=UTC)
                for index in range(13):
                    add(at + timedelta(minutes=5 * index), value)
            for minute in (0, 10, 20, 30, 40, 50):
                add(datetime(2026, 9, day, 11, minute, tzinfo=UTC), 90)
        add(datetime(2026, 9, 1, 9, 2, tzinfo=UTC), 100, quality="estimated")
        add(datetime(2026, 9, 1, 9, 3, tzinfo=UTC), 100, unit="wrong")
        add(datetime(2026, 9, 1, 9, 4, tzinfo=UTC), 100, source="synthetic-other-provider")
        add(datetime(2026, 9, 1, 9, 1, tzinfo=UTC), 101)
        add(datetime(2026, 9, 1, 9, 6, tzinfo=UTC), -1)
        for month, days in ((3, (28, 29, 30)), (10, (23, 24, 25))):
            for day in days:
                at = datetime(2026, month, day, tzinfo=UTC)
                for index in range(49):
                    add(at + timedelta(minutes=5 * index), 51 if index % 2 else 50.999999)


def exercise(engine, python, native):
    def compare(arguments, status=200, token=HEALTH):
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

    request = {"start": "2026-09-01", "end": "2026-09-03", "timezone": "UTC"}
    compare(request, 401, token=None)
    compare(request, 403, token=DIARY)
    result = compare(request)
    assert result["status"] == "ok"
    assert result["ranked_hours"][0]["hour"] == 9
    assert result["hours"][9]["elevated_share"] == 1
    assert result["hours"][16]["elevated_share"] == 0
    assert result["hours"][11]["observed_hours"] == 0
    assert result["hours"][8]["elevated_share"] is None
    compare(request | {"timezone": "Europe/Bratislava"})
    compare({key: value for key, value in request.items() if key != "timezone"})
    compare(request | {"timezone": None})
    compare(request | {"timezone": ""})
    for zone in ("Europe/Bratislava", "Asia/Kathmandu", "Australia/Lord_Howe"):
        compare({"start": "2026-03-28", "end": "2026-03-30", "timezone": zone})
        compare({"start": "2026-10-23", "end": "2026-10-25", "timezone": zone})
    skipped = compare({"start": "2011-12-30", "end": "2011-12-30", "timezone": "Pacific/Apia"})
    assert skipped["status"] == "insufficient_coverage"
    for bad in (
        {"end": "2026-10-02"},
        {"start": "2026-09-04"},
        {"timezone": "Invalid/Zone"},
        {"timezone": 1},
        {"unknown": True},
    ):
        compare(request | bad, 422)
    at = datetime(2027, 1, 1, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO measurements(ts,metric,source,local_date,value,unit,quality,details)
            SELECT :at+make_interval(secs=>i),'stress_score','garmin_connect',
                CAST(:at AS date),51,'score','observed','{}'::jsonb FROM generate_series(1,100001) i"""),
            {"at": at},
        )
    maximum = {"start": "2027-01-01", "end": "2027-01-31", "timezone": "UTC"}
    compare(maximum, 422)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM measurements WHERE ts=:at AND source='garmin_connect' AND metric='stress_score'"
            ),
            {"at": at + timedelta(seconds=100001)},
        )
    capped = compare(maximum)
    assert len(capped["hours"]) == 24 and capped["status"] == "insufficient_coverage"
    print(
        "Matched measured-hour ranking, sparse coverage, timezone transitions, skipped dates and 100000-sample limit"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed_stress(engine)
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
