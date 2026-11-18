"""Compare calendar-lag pairing and numerical correlations with pinned Python."""

import argparse
import json
import math
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
from capture_reference import isolated_engine
from check_metric_series import AT
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from garmin_ai.api import create_app
from garmin_ai.config import ApiToken, Settings
from garmin_ai.models import HealthDay

HEALTH = "synthetic-lagged-health-reader-validation-only"
DIARY = "synthetic-lagged-diary-reader-validation-only"
TOKENS = [{"key": HEALTH, "scopes": ["read:health"]}, {"key": DIARY, "scopes": ["read:diary"]}]
PATH = "/tools/analysis_lagged_association"


def seed_days(engine):
    with Session(engine) as session, session.begin():
        for index in range(3661):
            if index % 11 == 0:
                continue
            score = (index * 13) % 101
            session.add(
                HealthDay(
                    day=AT.date() + timedelta(days=index),
                    sleep_score=score if index % 13 else None,
                    stress_avg=100 - score if index % 7 else None,
                    resting_hr=60 + index % 40 + math.sin(index) / 10,
                    hydration_ml=index % 4000 if index % 9 else None,
                    steps=index * 97 % 10000,
                    vo2max=40,
                    sources={},
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
        if status == 422:
            return None
        expected, actual = [reply.json() for reply in replies]
        if status == 200:
            for first, second in zip(expected["results"], actual["results"], strict=True):
                if first["pearson_r"] is None:
                    assert second["pearson_r"] is None, (arguments, first, second)
                else:
                    assert second["pearson_r"] is not None
                    assert math.isclose(
                        first["pearson_r"], second["pearson_r"], rel_tol=1e-12, abs_tol=1e-12
                    ), (arguments, first, second)
                second["pearson_r"] = first["pearson_r"]
        assert expected == actual, (arguments, expected, actual)
        return actual

    request = {
        "metric_a": "sleep_score",
        "metric_b": "stress_avg",
        "start": AT.date().isoformat(),
        "end": (AT.date() + timedelta(days=60)).isoformat(),
        "lags": [-30, -7, -1, 0, 1, 7, 30],
    }
    compare(request, 401, token=None)
    compare(request, 403, token=DIARY)
    result = compare(request)
    assert result["results"][3]["pearson_r"] < -0.999999999999
    compare(request | {"lags": [7, -7, 0, 7]})
    compare(request | {"lags": []})
    compare(request | {"lags": ["1.0", True, False]})
    compare(request | {"lags": ["-1.0", -30.0, "+1.0", "-0.0"]})
    for value in ("-+1", "--1", "- 1"):
        compare(request | {"lags": [value]}, 422)
    compare(request | {"metric_a": "vo2max"})
    compare(request | {"metric_b": "vo2max"})
    compare(request | {"metric_a": "hydration_ml", "metric_b": "steps"})
    compare(request | {"metric_a": "resting_hr", "metric_b": "resting_hr", "lags": [0]})
    for size in (0, 9, 10, 11, 12, 13):
        sample = compare(
            request | {"end": (AT.date() + timedelta(days=size)).isoformat(), "lags": [0]}
        )
        if sample["results"][0]["n"] < 10:
            assert sample["results"][0]["pearson_r"] is None
    empty = compare(request | {"start": "2020-01-01", "end": "2020-01-20"})
    assert all(row["n"] == 0 and row["pearson_r"] is None for row in empty["results"])
    for invalid in (
        {"metric_a": "missing"},
        {"metric_b": "hrv_status"},
        {"lags": [31]},
        {"lags": [-31]},
        {"lags": list(range(16))},
        {"lags": [1.2]},
        {"start": request["end"], "end": request["start"]},
    ):
        compare(request | invalid, 422)
    maximum = compare(
        request
        | {"end": (AT.date() + timedelta(days=3660)).isoformat(), "lags": list(range(-7, 8))}
    )
    assert len(maximum["results"]) == 15 and maximum["results"][7]["n"] > 2000
    print(
        "Matched calendar gaps, exact pair counts, 1e-12 correlation tolerance and 3661-day/15-lag bounds"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        seed_days(engine)
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
