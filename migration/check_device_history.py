"""Compare the native FIT device view with pinned Python on disposable data."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from capture_reference import isolated_engine
from sqlalchemy import text
from sqlalchemy.orm import Session

from garmin_ai.device_history import history
from garmin_ai.models import Activity, ActivityPart, SourcePayload
from garmin_ai.normalize import PARSER_VERSION

START = datetime(2026, 10, 25, 9, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def add_activity(session, key, at, current=True, with_raw=True):
    parsed = f"synthetic/{key}.fit"
    session.add(
        Activity(
            id=key,
            start=at,
            end=at + HOUR,
            kind="running",
            timezone="UTC",
            fit_key=parsed if current else f"synthetic/{key}-new.fit",
            details={"parsed_fit_key": parsed},
        )
    )
    if with_raw:
        session.add(
            SourcePayload(
                source="garmin_connect",
                endpoint="activity_fit",
                source_key=key,
                payload_hash=f"test-{key}",
                archive_key=parsed,
                fetched_at=at,
                parser_version=PARSER_VERSION,
                status="normalized",
                payload=None,
            )
        )


def add_part(session, key, kind, index, **payload):
    session.add(ActivityPart(activity_id=key, kind=kind, sequence=index, payload=payload))


def seed(engine):
    with Session(engine) as session, session.begin():
        add_activity(session, "a", START)
        add_activity(session, "b", START, current=False)
        add_activity(session, "c", START, with_raw=False)
        add_activity(session, "d", START + HOUR)
        add_activity(session, "max-records", START + DAY)
        add_activity(session, "big-records", START + 2 * DAY)
        for index in range(100):
            add_activity(session, f"multi-{index:03}", START + 3 * DAY)
        session.flush()
        add_part(
            session,
            "a",
            "fit_device_info",
            0,
            device_index=0,
            software_version=4.2,
            manufacturer="🧪" * 80,
            sensor_position="🧪" * 81,
            hardware_version={"raw": "synthetic-private"},
            device_type=["unstructured"],
            serial_number="synthetic-private-serial",
            product="synthetic-private-model",
        )
        add_part(session, "a", "fit_hr_zone", 0, high_bpm=140, message_index=1, unknown=2)
        add_part(session, "a", "fit_hr_zone", 1, high_bpm=141)
        add_part(session, "a", "fit_hrm_profile", 2, enabled=False, log_hrv=True)
        add_part(session, "a", "fit_ohr_settings", 2, enabled=True)
        add_part(session, "a", "fit_zones_target", 3, max_heart_rate=180, hr_calc_type="custom")
        add_part(session, "a", "fit_unclassified", 0, manufacturer="synthetic-private-family")
        add_part(session, "b", "fit_hr_zone", 0, high_bpm=150)
        add_part(session, "d", "fit_hr_zone", 0, high_bpm=160)
        for index in range(201):
            add_part(session, "max-records", "fit_ohr_settings", index, enabled=True)
        for index in range(100):
            add_part(
                session,
                "big-records",
                "fit_device_info",
                index,
                manufacturer="🧪" * 80,
                software_version=6.42,
                source_type="🧪" * 80,
            )
            add_part(
                session,
                f"multi-{index:03}",
                "fit_device_info",
                0,
                manufacturer="テ" * 80,
                software_version=3.25,
            )
        session.execute(
            text("""
                INSERT INTO activity_parts(activity_id,kind,sequence,payload)
                VALUES('a','fit_device_info',4,jsonb_build_object(
                    'serial_number','synthetic-private-'||repeat('x',16777216),'device_index',4))
            """)
        )


def fixtures(engine):
    seed(engine)
    offset = timezone(2 * HOUR)
    cases = [
        ("both_ends", START, START + HOUR, 100),
        ("same_offset_range", START.astimezone(offset), (START + HOUR).astimezone(offset), 100),
        ("page_limit", START, START + HOUR, 2),
        ("exact_record_limit", START + DAY, START + DAY + HOUR, 100),
        ("trim_first_activity", START + 2 * DAY, START + 2 * DAY + HOUR, 100),
        ("trim_later_activities", START + 3 * DAY, START + 3 * DAY + HOUR, 100),
        ("empty", START - 2 * DAY, START - DAY, 200),
        ("limit_zero", START, START + HOUR, 0),
        ("limit_201", START, START + HOUR, 201),
        ("same_instant", START, START, 100),
        ("longer_than_366_days", START, START + 367 * DAY, 100),
        ("date_range", START, START + 368 * DAY, 100),
        ("reversed_dates", START, START - DAY, 100),
        ("near_calendar_edge", datetime(1, 1, 1, tzinfo=UTC), START, 100),
    ]
    results = []
    with Session(engine) as session:
        for name, start, end, limit in cases:
            result = error = None
            try:
                result = history(session, start, end, limit)
            except ValueError as exc:
                error = str(exc)
            results.append(
                {
                    "name": name,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": limit,
                    "expected": result,
                    "error": error,
                }
            )
    rows = results[0]["expected"]["rows"]
    assert [row["evidence_status"] for row in rows] == ["available", "stale", "unavailable"]
    assert len(rows[0]["records"][0]["fields"]["manufacturer"]) == 80
    assert all("synthetic-private" not in json.dumps(result) for result in results)
    assert (
        results[4]["expected"]["truncated"]
        and results[4]["expected"]["rows"][0]["records_truncated"]
    )
    assert results[5]["expected"]["truncated"] and len(results[5]["expected"]["rows"]) < 100
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        tests = fixtures(engine)
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "device-history.json"
            destination.write_text(json.dumps(tests), encoding="utf-8")
            subprocess.run(
                [args.binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_DEVICE_HISTORY": str(destination)},
            )
        print(f"Matched {len(tests)} device history responses against pinned Python")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
