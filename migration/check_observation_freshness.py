"""Compare native observation freshness with pinned Python and synthetic clocks."""

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.freshness import observation_freshness
from garmin_ai.models import HealthDay, Measurement, SourcePayload

AT = datetime(2026, 10, 25, 3, tzinfo=UTC)
HEART = UUID("11111111-1111-1111-1111-111111111111")
STRESS = UUID("22222222-2222-2222-2222-222222222222")
OTHER = UUID("33333333-3333-3333-3333-333333333333")
ZONE = "Europe/Bratislava"


def cases(engine):
    with Session(engine) as session, session.begin():
        for ref, endpoint in [(HEART, "heart_rate"), (STRESS, "stress"), (OTHER, "spo2")]:
            key = str(ref).replace("-", "") * 2
            session.add(
                SourcePayload(
                    id=ref,
                    source="garmin_connect",
                    endpoint=endpoint,
                    source_key="2026-10-25",
                    payload_hash=key,
                    archive_key="sha256:" + key,
                    fetched_at=AT,
                    payload={},
                    parser_version=9,
                    status="normalized",
                )
            )

        def add(metric, ts, ref, quality="observed", source="garmin_connect"):
            session.add(
                Measurement(
                    metric=metric,
                    ts=ts,
                    unit="synthetic",
                    local_date=ts.date(),
                    value=50,
                    source=source,
                    source_ref=ref,
                    quality=quality,
                )
            )

        for seconds in [1800, 1500, 1200, 900, 600, 300, 0, -60]:
            add("heart_rate_bpm", AT - timedelta(seconds=seconds), HEART)
        add("heart_rate_bpm", AT - timedelta(hours=5, seconds=90), HEART)
        add("heart_rate_bpm", AT - timedelta(hours=5) + timedelta(seconds=90), HEART)
        add("stress_score", AT - timedelta(minutes=85), STRESS)
        add("stress_score", AT - timedelta(minutes=80), STRESS)
        add("stress_score", AT - timedelta(minutes=12), STRESS)
        add("stress_score", AT - timedelta(minutes=7, microseconds=250), STRESS)
        add("body_battery", AT - timedelta(minutes=25), STRESS)
        add("body_battery", AT - timedelta(minutes=10), STRESS)
        add("respiration_rpm", AT - timedelta(days=3), None)
        add("spo2_pct", AT - timedelta(minutes=1), OTHER, quality="estimated")
        add("spo2_pct", AT - timedelta(minutes=1), OTHER, source="synthetic")
        add("spo2_pct", AT + timedelta(minutes=1), OTHER)

        session.add_all(
            [
                HealthDay(
                    day=date(2026, 10, 23),
                    hrv_nightly_avg=42,
                    sources={"field:hrv_nightly_avg": str(STRESS)},
                ),
                HealthDay(
                    day=date(2026, 10, 24),
                    sleep_score=80,
                    training_readiness_score=55,
                    sources={"field:training_readiness_score": str(HEART)},
                ),
                HealthDay(day=date(2026, 10, 27), hrv_nightly_avg=99, sleep_score=99),
            ]
        )
        session.flush()

        good = {
            "heart_rate": {
                "status": "normalized",
                "source_ref": str(HEART),
                "fetch_lag_seconds": 10.25,
                "source_updated_at": "2026-10-25T02:58:00+00:00",
            },
            "stress": {"status": "unchanged", "source_ref": str(STRESS)},
            "spo2": {"status": "normalized", "source_ref": str(OTHER)},
        }
        wrong = {**good, "heart_rate": {"status": "normalized", "source_ref": str(OTHER)}}
        failed = {
            "heart_rate": {"status": "fetch_error"},
            "stress": {"status": "error", "source_ref": "unverified-reference"},
            "spo2": {"status": "empty"},
        }
        observed = observation_freshness(session, AT, ZONE, good)
        assert observed["heart_rate_bpm"]["quality_reason"] == "recent_observations"
        assert observed["respiration_rpm"]["quality_reason"] == "stale_observation"
        assert observed["spo2_pct"]["quality_reason"] == "source_empty"

        variants = [
            (AT, ZONE, good),
            (AT, ZONE, wrong),
            (AT, ZONE, failed),
            (AT, ZONE, {}),
            (AT, ZONE, {"spo2": {"status": "unchanged"}}),
            (AT + timedelta(minutes=2), ZONE, good),
            (AT + timedelta(hours=2), ZONE, good),
            (AT - timedelta(hours=5), ZONE, {}),
            (AT, "UTC", {}),
            (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), "America/Havana", good),
            (datetime(2026, 3, 8, 5, 30, tzinfo=UTC), "America/Havana", {}),
        ]
        return [
            [at.isoformat(), zone, endpoints, observation_freshness(session, at, zone, endpoints)]
            for at, zone, endpoints in variants
        ]


def run(binary):
    engine = isolated_engine()
    try:
        fixture = cases(engine)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "observation-freshness.json"
            path.write_text(json.dumps(fixture, allow_nan=False), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_OBSERVATION_FRESHNESS": str(path)},
            )
        print(f"Matched {len(fixture)} freshness contexts with eight channels each")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
