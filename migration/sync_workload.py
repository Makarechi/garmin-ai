"""Measure synthetic Garmin normalization and durable history using the Python baseline."""

import argparse
import json
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time

from fit_workload import archive_input
from sqlalchemy import func, select

from garmin_ai.accounts import bind_account, profile_fingerprint
from garmin_ai.archive import LocalArchive
from garmin_ai.backfill import history_status, schedule_history
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.garmin_contract import ENDPOINTS
from garmin_ai.jobs import claim, finish
from garmin_ai.models import Activity, ActivityPart, HealthDay, Measurement, SourcePayload
from garmin_ai.scenario_packs import PackSelection, configure_scenario_pack, ensure_scenario_packs
from garmin_ai.sync import run_garmin_job

DAY = date(2026, 10, 24)
HISTORY_NOW = datetime(2026, 10, 25, 9, tzinfo=UTC)
ACTIVITY_ID = "123456"


def fake_day(endpoint, day):
    morning = datetime.combine(day, clock_time(hour=4), tzinfo=UTC)
    times = [morning + timedelta(minutes=5 * index) for index in range(24)]
    millis = [int(moment.timestamp() * 1000) for moment in times]
    sample_fields = {
        "heart_rate": ("heartRateValues", 70),
        "stress": ("stressValuesArray", 30),
        "respiration": ("respirationValuesArray", 15),
        "spo2": ("spO2ValuesArray", 98),
    }
    if endpoint in sample_fields:
        key, base = sample_fields[endpoint]
        return {key: [[value, base + index % 2] for index, value in enumerate(millis)]}
    if endpoint == "steps":
        return [
            {"startGMT": value.isoformat(), "steps": index} for index, value in enumerate(times)
        ]
    if endpoint == "hrv":
        return {
            "hrvSummary": {"lastNightAvg": 52},
            "hrvReadings": [
                {"readingTimeGMT": value.isoformat(), "hrvValue": 52} for value in times
            ],
        }
    if endpoint == "sleep":
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": 25200,
                "sleepStartTimestampGMT": (morning - timedelta(hours=7)).isoformat(),
                "sleepEndTimestampGMT": morning.isoformat(),
                "sleepScores": {"overall": {"value": 82}},
            },
        }
    known = {
        "daily": {"totalSteps": 3600 + day.day, "activeKilocalories": 250},
        "readiness": {"score": 73, "timestamp": morning.isoformat(), "recoveryTime": 0},
        "body_battery": {"date": day.isoformat(), "charged": 55, "drained": 36},
        "hydration": {"valueInML": 1500},
        "max_metrics": {"generic": {"vo2MaxPreciseValue": 52.6}},
    }
    return known.get(endpoint, {"synthetic": endpoint, "date": day.isoformat()})


class SyntheticReader:
    def __init__(self):
        self.calls = Counter()
        self.fingerprint = profile_fingerprint({"profileId": 12345})

    def account_fingerprint(self):
        return self.fingerprint

    def fetch(self, endpoint, *, day=None, activity_id=None):
        self.calls[endpoint.name] += 1
        if endpoint.scope == "day":
            return fake_day(endpoint.name, day)
        if endpoint.name == "activity":
            return self.activity()
        fixture = {"synthetic": endpoint.name}
        if activity_id is not None:
            fixture["activityId"] = activity_id
        return fixture

    @staticmethod
    def activity():
        return {
            "activityId": int(ACTIVITY_ID),
            "startTimeGMT": f"{DAY.isoformat()}T15:00:00Z",
            "duration": 120,
            "distance": 500,
            "activityType": {"typeKey": "running"},
        }

    def call(self, method, *args, **kwargs):
        self.calls[method] += 1
        if method == "get_activities" and args == (0, 100):
            return [self.activity()]
        if method == "download_activity" and args == (ACTIVITY_ID,):
            return archive_input(120)[0]
        raise ValueError(f"Unsupported synthetic Garmin request {method}")


def configure_synthetic_owner(engine, reader, archive):
    with transaction(engine) as session:
        bind_account(
            session,
            reader.account_fingerprint(),
            archive_root=archive.root,
            confirm_existing_owner=True,
        )
        for name, pack in ensure_scenario_packs(session).items():
            if pack.collection_enabled:
                continue
            configure_scenario_pack(
                session,
                name,
                PackSelection(
                    revision=pack.revision,
                    tracking_enabled=pack.tracking_enabled,
                    collection_enabled=True,
                    reminders_enabled=pack.reminders_enabled,
                    visible=pack.visible,
                    llm_enabled=pack.llm_enabled,
                    outcome_goal=pack.outcome_goal,
                ),
            )


def sync_day(engine, reader, archive, settings):
    def dispatch(kind, **payload):
        run_garmin_job(engine, reader, archive, settings, kind, payload)

    dispatch("garmin_activities", offset=0, since=DAY.isoformat())
    for endpoint in ENDPOINTS:
        key = {"day": DAY.isoformat(), "global": "global", "activity": ACTIVITY_ID}[endpoint.scope]
        dispatch("garmin_endpoint", endpoint=endpoint.name, key=key)
    dispatch("garmin_fit", activity_id=ACTIVITY_ID)
    if len(reader.calls) != len(ENDPOINTS) + 2 or any(
        count != 1 for count in reader.calls.values()
    ):
        raise RuntimeError("The synthetic day did not cover every Garmin entry point exactly once")


def claim_history(engine):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with transaction(engine) as session:
            job = claim(session, kinds=["garmin_endpoint"])
            if job is not None:
                return job.id, job.lease_token, dict(job.payload)
        time.sleep(0.05)
    raise RuntimeError("History job could not be claimed")


def backfill(engine, reader, archive, settings, days, preload):
    day_endpoints = sum(endpoint.scope == "day" for endpoint in ENDPOINTS)

    def drain(count):
        for _ in range(count):
            identity, lease, payload = claim_history(engine)
            run_garmin_job(engine, reader, archive, settings, "garmin_endpoint", payload)
            with transaction(engine) as session:
                finish(session, identity, lease)

    for offset in range(0, days, 2):
        with transaction(engine) as session:
            schedule_history(session, settings, HISTORY_NOW)
        if not preload:
            drain(min(days - offset, 2) * day_endpoints)
    if preload:
        drain(days * day_endpoints)
    with transaction(engine) as session:
        status = history_status(session)
    windows = status["windows"]
    if set(windows) != {"archived", "normalized"} or sum(windows.values()) != days * day_endpoints:
        raise RuntimeError("Historical window counts do not match the workload")
    return windows


def run(days, preload):
    settings = Settings(_env_file=None, backfill_days=days, timezone="UTC")
    engine = make_engine(settings)
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("The sync workload requires a dedicated synthetic test database")
    archive = LocalArchive(settings.data_dir / "raw")
    reader = SyntheticReader()
    try:
        configure_synthetic_owner(engine, reader, archive)
        start = time.monotonic()
        if days == 1:
            sync_day(engine, reader, archive, settings)
            windows = {}
        else:
            windows = backfill(engine, reader, archive, settings, days, preload)
        duration = time.monotonic() - start
        with transaction(engine) as session:
            counts = {
                model.__tablename__: session.scalar(select(func.count()).select_from(model))
                for model in (SourcePayload, HealthDay, Measurement, Activity, ActivityPart)
            }
        expected_sources = (
            len(ENDPOINTS) + 2
            if days == 1
            else days * sum(endpoint.scope == "day" for endpoint in ENDPOINTS)
        )
        if (
            counts["source_payloads"] != expected_sources
            or counts["health_days"] != days
            or counts["measurements"] != 144 * days
        ):
            raise RuntimeError(f"Unexpected committed workload counts: {counts}")
        if days == 1 and (counts["activities"], counts["activity_parts"]) != (1, 125):
            raise RuntimeError("Unexpected activity part counts")
        return {
            "days": days,
            "history_queue": "preloaded" if preload else "drained between scheduler passes",
            "duration_seconds": round(duration, 3),
            "fetch_counts": dict(sorted(reader.calls.items())),
            "rows": counts,
            "historical_windows": windows,
        }
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("days", type=int, choices=(1, 7, 30, 365))
    parser.add_argument("--preload-history", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.days, args.preload_history)))
