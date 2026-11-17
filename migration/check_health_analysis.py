"""Compare daily measurement descriptions and period estimates against pinned NumPy."""

import argparse
import json
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from sqlalchemy import insert
from sqlalchemy.orm import Session

from garmin_ai.analytics import compare_periods, personal_baseline
from garmin_ai.models import HealthDay
from garmin_ai.queries import HEALTH_METRICS

ORIGIN = date(1997, 12, 21)


def window(start, length):
    return ORIGIN + timedelta(days=start), ORIGIN + timedelta(days=start + length - 1)


def encode(period):
    return [[day.year, day.month, day.day] for day in period]


def seed(engine):
    with Session(engine) as session, session.begin():
        for start in range(0, 7322, 400):
            rows = []
            for index in range(start, min(7322, start + 400)):
                fields = {
                    "day": ORIGIN + timedelta(days=index),
                    "steps": 4_000 + (index * 19) % 9_001,
                    "sleep_score": None if index % 9 == 4 else 56 + ((index * 71) % 433) / 10,
                    "stress_avg": 33.0,
                }
                if index < 6:
                    fields["resting_hr"] = [50, None, float("nan"), 62, float("inf"), 70][index]
                if index % 4 != 0:
                    rows.append(fields)
            session.execute(insert(HealthDay), rows)


def fixture_cases(engine):
    with Session(engine) as session:
        cases = [
            {
                "metric": metric,
                "first": encode(window(0, 41)),
                "expected": personal_baseline(session, metric, *window(0, 41)),
            }
            for metric in sorted(HEALTH_METRICS)
        ]
        boundary = (date.min + timedelta(days=60),) * 2
        last = (date.max - timedelta(days=60),) * 2
        for day in [boundary, last, window(1, 1), window(7600, 31)]:
            cases.append(
                {
                    "metric": "steps",
                    "first": encode(day),
                    "expected": personal_baseline(session, "steps", *day),
                }
            )
        for metric in ["steps", "sleep_score", "stress_avg"]:
            for first, second in [
                (window(0, 41), window(60, 44)),
                (window(2, 1), window(8, 2)),
                (window(2, 1), window(7600, 22)),
                (window(0, 3661), window(3661, 3661)),
                (window(1980, 48), window(30, 71)),
                (window(1, 22), window(90, 19)),
                (window(1, 20), window(40, 20)),
                (window(1, 19), window(40, 19)),
            ]:
                cases.append(
                    {
                        "metric": metric,
                        "first": encode(first),
                        "second": encode(second),
                        "expected": compare_periods(session, metric, *first, *second),
                    }
                )
        return cases


def run(binary):
    engine = isolated_engine()
    try:
        seed(engine)
        cases = fixture_cases(engine)
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / "analysis.json"
            path.write_text(json.dumps(cases, allow_nan=False), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored"],
                env={**os.environ, "GA_TEST_ANALYSIS_CASES": str(path)},
                check=True,
            )
        print(f"Native health analysis matches {len(cases)} cases from pinned Python and NumPy")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
