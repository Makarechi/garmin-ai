"""Check when native pre-event evidence is observed and when it becomes knowable."""

import argparse
import json
import os
import struct
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.models import MetricObservation
from garmin_ai.temporal import FEATURE_VERSION, features_at

EVENT = datetime(2026, 10, 25, 9, tzinfo=UTC)
SOURCE = UUID("11111111-1111-1111-1111-111111111111")
METRICS = ("training_readiness_score", "sleep_score", "hrv_nightly_avg")


def seed(engine):
    with Session(engine) as session, session.begin():

        def add(
            sequence,
            metric,
            value,
            observed,
            fetched,
            ingested,
            *,
            effective_start=None,
            quality="observed",
            feature_version=FEATURE_VERSION,
            valid=True,
        ):
            session.add(
                MetricObservation(
                    id=UUID(int=sequence + 1),
                    source_ref=SOURCE,
                    metric=metric,
                    unit="synthetic",
                    value=value,
                    source_calendar_date=date(2026, 10, 26),
                    observed_at=observed,
                    effective_start=effective_start,
                    fetched_at=fetched,
                    ingested_at=ingested,
                    timezone="Europe/Bratislava",
                    quality=quality,
                    sequence=sequence,
                    feature_version=feature_version,
                    valid=valid,
                )
            )

        older = EVENT - timedelta(hours=2)
        newer = EVENT - timedelta(hours=1)
        late = EVENT + timedelta(hours=1)
        add(1, METRICS[0], 70, older, newer, newer, valid=False)
        add(2, METRICS[0], 60, older, EVENT, late)
        add(3, METRICS[0], 65, older, EVENT, late)
        add(4, METRICS[0], 90, EVENT, EVENT, EVENT)
        add(5, METRICS[0], 91, newer, newer, EVENT, quality="estimated")
        add(6, METRICS[0], 92, newer, newer, EVENT, feature_version="other")
        add(7, METRICS[0], 93, None, newer, EVENT, quality="time_unknown")
        add(
            8,
            METRICS[1],
            80,
            newer,
            newer,
            EVENT,
            effective_start=EVENT - timedelta(hours=8),
        )
        boundary = EVENT - timedelta(hours=24)
        add(9, METRICS[2], -0.0, boundary, boundary, boundary)


def large_context(engine):
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO metric_observations(
              id, source_ref, metric, unit, value, source_calendar_date, observed_at,
              fetched_at, ingested_at, timezone, quality, sequence, feature_version,
              valid
            )
            SELECT gen_random_uuid(), :source, 'synthetic_load', 'test', n, DATE '2026-10-25',
                   :observed, :observed,
                   CAST(:ingested AS timestamptz) + CASE WHEN n > 99997
                       THEN interval '1 minute' ELSE interval '0 seconds' END,
                   'UTC', 'observed', n, :version, true
            FROM generate_series(1,100001) AS n
            """),
            {
                "source": SOURCE,
                "observed": EVENT - timedelta(minutes=1),
                "ingested": EVENT,
                "version": FEATURE_VERSION,
            },
        )


def comparable(value):
    if isinstance(value, float):
        return ("float64", struct.pack(">d", value).hex())
    if isinstance(value, list):
        return [comparable(item) for item in value]
    if isinstance(value, dict):
        return {key: comparable(item) for key, item in value.items()}
    return value


def compare_cases(binary, engine, cases, label):
    requests = []
    expected = []
    with Session(engine) as session:
        for metrics, events, cutoff, purpose in cases:
            try:
                reference = features_at(session, metrics, events, cutoff, purpose)
                expected.append([[at.isoformat(), data] for at, data in sorted(reference.items())])
            except (ValueError, OverflowError) as error:
                expected.append({"error": str(error), "class": type(error).__name__})
            requests.append(
                {
                    "metrics": metrics,
                    "events": [event.isoformat() for event in events],
                    "cutoff": cutoff.isoformat(),
                    "purpose": purpose,
                }
            )
    with TemporaryDirectory() as directory:
        path = Path(directory) / "temporal-features.json"
        path.write_text(json.dumps(requests), encoding="utf-8")
        result = subprocess.run(
            [binary, "--ignored", "--exact", "select_feature_evidence", "--nocapture"],
            check=False,
            text=True,
            capture_output=True,
            env={**os.environ, "GA_TEST_TEMPORAL_FEATURES": str(path)},
        )
        assert result.returncode == 0, (label, result.stderr, result.stdout[-2000:])
        output = result.stdout
        prefix = "temporal: "
        actual = [
            json.loads(line.removeprefix(prefix))
            for line in output.splitlines()
            if line.startswith(prefix)
        ]
    assert len(actual) == len(expected)
    for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
        assert comparable(left) == comparable(right), (label, index, left, right)
    print("Matched temporal batches", label, len(actual), flush=True)
    return expected


def run(binary):
    engine = isolated_engine()
    try:
        seed(engine)
        large_context(engine)
        cases = [
            (METRICS, [EVENT], EVENT, "as_known"),
            (METRICS, [EVENT], EVENT + timedelta(hours=1), "retrospective"),
            (METRICS, [EVENT], EVENT - timedelta(microseconds=1), "as_known"),
            (
                (*METRICS, "unknown_metric", *METRICS),
                [EVENT, EVENT + timedelta(microseconds=1), EVENT, EVENT - timedelta(hours=30)],
                EVENT + timedelta(hours=12),
                "retrospective",
            ),
            (METRICS, [EVENT + timedelta(days=2)], EVENT, "as_known"),
            (METRICS, [EVENT], EVENT + timedelta(microseconds=1), "as_known"),
            ((), [EVENT, EVENT], EVENT, "as_known"),
            (METRICS, [], EVENT, "as_known"),
            (("synthetic_load",), [EVENT], EVENT + timedelta(minutes=1), "retrospective"),
            (("synthetic_load",), [EVENT], EVENT - timedelta(microseconds=1), "as_known"),
            (
                ("synthetic_load", METRICS[0], METRICS[1]),
                [EVENT + timedelta(microseconds=i) for i in range(2000)],
                EVENT,
                "as_known",
            ),
        ]
        expected = compare_cases(
            binary, engine, cases, "original history and 100001-version limits"
        )
        assert expected[0][0][1][METRICS[0]]["value"] == 70
        assert expected[1][0][1][METRICS[0]]["value"] == 65
        assert expected[1][0][1][METRICS[2]]["value"] == 0
        assert expected[2][0][1][METRICS[1]]["value"] is None
        assert "100000" in expected[8]["error"]
        assert len(expected[10]) == 2000
        assert all(metrics["synthetic_load"]["value"] == 99997 for _, metrics in expected[10])
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
