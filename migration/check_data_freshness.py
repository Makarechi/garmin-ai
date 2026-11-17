"""Compare the combined native freshness report with pinned Python."""

import argparse
import json
import math
import os
import subprocess
from datetime import timedelta
from pathlib import Path

from capture_reference import isolated_engine
from check_history_sync_status import seed as history_seed
from check_observation_freshness import AT, HEART, OTHER, STRESS, ZONE
from check_observation_freshness import cases as observation_seed
from sqlalchemy.orm import Session

from garmin_ai.models import AppState, Job, SourcePayload
from garmin_ai.queries import data_freshness


def freshness_seed(engine):
    def indicator(endpoint, key, stored_key=None, **value):
        return AppState(
            key=f"freshness:{endpoint}:{stored_key or key}", value={"source_key": key, **value}
        )

    with Session(engine) as session, session.begin():
        session.get(SourcePayload, HEART).source_updated_at = AT - timedelta(
            minutes=8, microseconds=775
        )
        session.add_all(
            [
                indicator(
                    "heart_rate",
                    "2026-10-25",
                    stored_key="a",
                    fetched_at="2026-10-25T03:00:14.000015+00:00",
                    success_at=AT.isoformat(),
                    source_ref=HEART.hex,
                    status="normalized",
                ),
                indicator(
                    "heart_rate",
                    "2026-10-25",
                    stored_key="b",
                    fetched_at="2026-10-25T06:00:14.000015+03:00",
                    status="fetch_error",
                ),
                indicator(
                    "heart_rate",
                    "2026-10-24",
                    fetched_at="2026-10-25T11:59:45+09:00",
                    source_ref=str(OTHER),
                    status="empty",
                ),
                indicator(
                    "stress",
                    "2026-10-25",
                    success_at=(AT - timedelta(minutes=2)).isoformat(),
                    status="unchanged",
                    source_ref=str(STRESS),
                ),
                indicator(
                    "devices",
                    "global",
                    success_at=(AT + timedelta(minutes=10)).isoformat(),
                    status="empty",
                ),
                indicator(
                    "activities",
                    "page:100",
                    fetched_at=AT.isoformat(),
                    source_ref="44444444-4444-4444-4444-444444444444",
                    status="error",
                ),
                indicator("sleep", "2026-10-23", status="not_attempted"),
                AppState(
                    key="integration:garmin", value={"status": "ok", "checked": AT.isoformat()}
                ),
            ]
        )


def check(binary, engine, clock, zone, stage):
    with Session(engine, info={"timezone": zone}) as session:
        expected = data_freshness(session, now=clock)
    result = subprocess.run(
        [binary, "--ignored", "--nocapture"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GA_TEST_FRESHNESS_CLOCK": clock.isoformat(),
            "GA_TEST_FRESHNESS_ZONE": zone,
        },
    )
    prefix = "freshness: "
    [document] = [
        line.removeprefix(prefix) for line in result.stdout.splitlines() if line.startswith(prefix)
    ]
    actual = json.loads(document)
    for response in (actual, expected):
        history = response["history_sync"]
        history["activity_scans"].sort(key=lambda scan: json.dumps(scan, sort_keys=True))
        history["plans"].sort(key=lambda plan: json.dumps(plan, sort_keys=True))
    assert_same(actual, expected, stage)
    return actual


def assert_same(actual, expected, path):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), path
        for key, value in expected.items():
            assert_same(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), path
        for index, value in enumerate(expected):
            assert_same(actual[index], value, f"{path}[{index}]")
    elif type(expected) is float:
        assert math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12), path
    else:
        assert actual == expected, (path, actual, expected)


def run(binary):
    engine = isolated_engine()
    try:
        empty = check(binary, engine, AT, ZONE, "empty")
        assert not empty["available"] and len(empty["channels"]) == 8
        observation_seed(engine)
        history_seed(engine)
        freshness_seed(engine)
        report = check(binary, engine, AT, ZONE, "ready")
        assert report["available"] and len(report["channels"]) == 8
        assert report["endpoints"]["heart_rate"]["source_ref"] == str(HEART)
        assert report["historical"]["heart_rate"]["source_ref"] == str(OTHER)
        assert report["endpoints"]["heart_rate"]["fetch_lag_seconds"] == 0
        assert sum(report["history_sync"]["windows"].values()) == 7300
        check(binary, engine, AT + timedelta(minutes=18), "UTC", "positive fetch lag")
        check(binary, engine, AT, "Pacific/Honolulu", "different local date")
        with Session(engine) as session, session.begin():
            session.get(SourcePayload, HEART).parser_version = 8
            session.add(
                Job(
                    kind="raw_replay",
                    payload={"target_version": 9},
                    dedup_key="freshness:synthetic:replay",
                    status="pending",
                    run_at=AT,
                )
            )
            session.add(
                AppState(
                    key="replay:synthetic:9",
                    value={"status": "error", "target_version": 9},
                )
            )
        pending = check(binary, engine, AT, ZONE, "replay pending")
        assert not pending["available"] and pending["channels"] == {}
        assert pending["endpoints"]["heart_rate"]["parser_version"] == 8
        assert pending["archive_replay"]["jobs"] == {"pending": 1}
        assert pending["archive_replay"]["outcomes"] == {"error": 1}
        with Session(engine) as session, session.begin():
            session.get(SourcePayload, HEART).parser_version = 9
        restored = check(binary, engine, AT, ZONE, "replay restored")
        assert restored["available"] and len(restored["channels"]) == 8
        print("Matched six complete reports against Python, including 7,300 history windows")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
