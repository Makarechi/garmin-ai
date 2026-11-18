"""Compare source-separated measurement aggregation and its coverage with pinned Python."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
from capture_reference import isolated_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.models import Measurement

HEALTH = "synthetic-metric-series-health-reader"
DIARY = "synthetic-metric-series-diary-reader"
TOKENS = [{"key": HEALTH, "scopes": ["read:health"]}, {"key": DIARY, "scopes": ["read:diary"]}]
AT = datetime(2026, 10, 25, 9, tzinfo=UTC)
PATH = "/tools/metric_series"


def seed(engine):
    with Session(engine) as session, session.begin():
        sequence = 0

        def add(source, seconds, value, *, metric="heart_rate_bpm", unit="bpm", quality="observed"):
            nonlocal sequence
            sequence += 1
            at = AT + timedelta(seconds=seconds)
            session.add(
                Measurement(
                    ts=at,
                    local_date=at.date(),
                    source=source,
                    metric=metric,
                    unit=unit,
                    value=value,
                    quality=quality,
                    source_ref=UUID(int=sequence),
                )
            )

        for source, cases in (
            ("coverage:80", [(0, 100), (48, 120)]),
            ("coverage:below", [(0, 80), (47.999999, 100)]),
            ("cross:edge", [(-60, 70), (30, 100), (90, 110)]),
            ("gap:unknown", [(0, 100), (301, 120)]),
            ("quality:filtered", [(0, 80), (60, 120)]),
            ("source:Я", [(0, 100)]),
            ("source:é", [(0, 110)]),
        ):
            for seconds, value in cases:
                add(source, seconds, value)
        add("quality:filtered", 30, 200, quality="estimated")
        add("canonical:wrong-unit", 0, 100, unit="missing")
        add("canonical:below", 0, 0)
        add("canonical:above", 0, 301)
        add("canonical:infinity", 0, float("inf"))
        add("canonical:nan", 0, float("nan"))
        for source, values in (("steps:first", [10, 20, 30]), ("steps:second", [3, 4, 5])):
            for index, value in enumerate(values):
                add(source, index * 30, value, metric="steps_bucket", unit="steps")
        for index in range(110):
            add("provenance:bounded", index / 10, 70 + index % 10)
        add("sparse:spo2", 0, 95, metric="spo2_pct", unit="%")
        add("sparse:spo2", 3000, 97, metric="spo2_pct", unit="%")
        for at in (
            datetime(1969, 12, 31, 23, 59, 30, tzinfo=UTC),
            datetime(1970, 1, 1, 0, 0, 30, tzinfo=UTC),
        ):
            session.add(
                Measurement(
                    ts=at,
                    local_date=at.date(),
                    source="negative:epoch",
                    metric="respiration_rpm",
                    unit="rpm",
                    value=20,
                    quality="observed",
                )
            )


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

    request = {
        "metric": "heart_rate_bpm",
        "start": AT.isoformat(),
        "end": (AT + timedelta(minutes=1)).isoformat(),
        "minutes": 1,
    }
    compare(request, 401, token=None)
    compare(request, 403, token=DIARY)
    result = compare(request)
    rows = {row["source"]: row for row in result["rows"]}
    assert rows["coverage:80"]["mean"] == 100 and rows["coverage:80"]["coverage_ratio"] == 0.8
    assert rows["coverage:below"]["mean"] is None
    assert rows["cross:edge"]["mean"] == 85 and rows["cross:edge"]["samples"] == 1
    assert len(rows["cross:edge"]["source_refs"]) == 3
    assert rows["gap:unknown"]["mean"] is None and rows["gap:unknown"]["covered_seconds"] == 0
    assert rows["quality:filtered"]["mean"] == 80
    assert len(rows["provenance:bounded"]["source_refs"]) == 100
    assert rows["provenance:bounded"]["source_refs_truncated"]
    assert all(not source.startswith("canonical:") for source in rows)
    steps = compare(request | {"metric": "steps_bucket"})
    assert [row["sum"] for row in steps["rows"]] == [30, 7]
    assert all(
        row["covered_seconds"] is None and row["coverage_ratio"] is None for row in steps["rows"]
    )
    compare(request | {"start": "2026-10-25T14:30:00+05:30", "end": "2026-10-25T14:31:00+05:30"})
    compare(
        request | {"start": "2026-10-25T09:00:00.120000Z", "end": "2026-10-25T09:01:00.120000Z"}
    )
    compare(request | {"minutes": "1.0"})
    compare(request | {"minutes": True})
    compare(request | {"minutes": 1440})
    compare({key: value for key, value in request.items() if key != "minutes"})
    compare(
        request
        | {"start": AT.timestamp() * 1000, "end": (AT + timedelta(minutes=1)).timestamp() * 1000}
    )
    sparse = compare(
        request
        | {
            "metric": "spo2_pct",
            "start": (AT + timedelta(minutes=20)).isoformat(),
            "end": (AT + timedelta(minutes=21)).isoformat(),
        }
    )
    assert sparse["rows"][0]["samples"] == 0 and sparse["rows"][0]["mean"] == 95
    compare(
        request
        | {
            "metric": "respiration_rpm",
            "start": "1969-12-31T23:59:25Z",
            "end": "1970-01-01T00:00:25Z",
        }
    )
    compare(request | {"metric": "hydration_ml"}, 422)
    compare(request | {"metric": "missing"}, 422)
    compare(request | {"minutes": 0}, 422)
    compare(request | {"minutes": 1441}, 422)
    compare(request | {"end": AT.isoformat()}, 422)
    empty = compare(
        request
        | {
            "start": (AT - timedelta(days=2)).isoformat(),
            "end": (AT - timedelta(days=1)).isoformat(),
        }
    )
    assert empty["rows"] == [] and not empty["truncated"]
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO measurements(ts,metric,source,local_date,value,unit,quality,details)
            SELECT :at,'stress_score',concat('synthetic-source:',lpad(i::text,4,'0')),
                CAST(:at AS date),1,'score','observed','{}'::jsonb FROM generate_series(1,2001) i
        """),
            {"at": AT},
        )
    capped = compare(request | {"metric": "stress_score"})
    assert len(capped["rows"]) == 2000 and capped["truncated"]
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO measurements(ts,metric,source,local_date,value,unit,quality,details)
            SELECT :at+make_interval(secs=>i),'hrv_rmssd_ms','synthetic-cap',CAST(:at AS date),
                1,'ms','observed','{}'::jsonb FROM generate_series(1,100001) i
        """),
            {"at": AT},
        )
    compare(request | {"metric": "hrv_rmssd_ms", "end": (AT + timedelta(days=2)).isoformat()}, 422)
    with engine.begin() as connection:
        connection.execute(
            text("""DELETE FROM measurements WHERE metric='hrv_rmssd_ms'
                AND source='synthetic-cap' AND ts=:at"""),
            {"at": AT + timedelta(seconds=100001)},
        )
    maximum = compare(
        request | {"metric": "hrv_rmssd_ms", "end": (AT + timedelta(days=2)).isoformat()}
    )
    assert len(maximum["rows"]) == 1667 and not maximum["truncated"]
    print(
        "Matched weighted coverage, source and provenance boundaries, offset display, 2000-row truncation and sample cap"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed(engine)
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
