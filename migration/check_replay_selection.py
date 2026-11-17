"""Compare the bounded Rust replay inventory with the pinned Python selector."""

import argparse
import json
import os
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import String, cast, select
from sqlalchemy.orm import Session

from garmin_ai import replay
from garmin_ai.models import Activity, ActivityPart, AppState, HealthDay, SourcePayload

BASE = datetime(2026, 10, 25, tzinfo=UTC)
ACCOUNT = "a" * 64
SCOPE = "2026-10-25"


def seed(session):
    def state(key, value):
        session.add(AppState(key=key, value=value))

    def raw(
        number,
        endpoint,
        key,
        *,
        status="normalized",
        version=8,
        source="garmin_connect",
        payload=None,
    ):
        identity = UUID(int=number)
        digest = f"{number:064x}"
        suffix = "fit" if endpoint == "activity_fit" else "json"
        row = SourcePayload(
            id=identity,
            source=source,
            endpoint=endpoint,
            source_key=key,
            status=status,
            parser_version=version,
            fetched_at=BASE + timedelta(seconds=number),
            payload=payload if payload is not None else {},
            payload_hash=digest,
            archive_key=f"00/{digest}.{suffix}",
        )
        session.add(row)
        return row

    state("account:garmin", {"fingerprint": ACCOUNT})
    older = raw(1, "daily", SCOPE)
    raw(2, "daily", SCOPE)
    latest = raw(3, "daily", SCOPE)
    state(
        f"ingest:garmin_connect:daily:{SCOPE}",
        {"source_ref": str(latest.id), "requested_at": latest.fetched_at.isoformat()},
    )
    session.add(
        HealthDay(day=date.fromisoformat(SCOPE), steps=100, sources={"field:steps": str(older.id)})
    )

    good_fit = raw(4, "activity_fit", "synthetic-fit")
    failed_fit = raw(5, "activity_fit", "synthetic-fit", status="error")
    recorded_failure = raw(6, "activity_fit", "synthetic-fit", status="error")
    raw(7, "activity_fit", "synthetic-no-activity")
    state(f"ingest-meta:{recorded_failure.id}", {"failed_parser_version": 9})
    state(
        "fit-version:synthetic-fit",
        {
            "source_ref": str(good_fit.id),
            "requested_at": good_fit.fetched_at.isoformat(),
            "latest_attempt": {"source_ref": str(failed_fit.id)},
        },
    )

    def activity(identity, **optional):
        session.add(
            Activity(
                id=identity, kind="synthetic", start=BASE, end=BASE, timezone="UTC", **optional
            )
        )

    activity(
        "synthetic-fit",
        fit_key=good_fit.archive_key,
        details={"parsed_fit_key": good_fit.archive_key, "fit_status": "normalized"},
    )
    activity("synthetic-old-summary")
    summary = raw(8, "activities", "page:0", payload=[{"activityId": "synthetic-old-summary"}])
    state("ingest:garmin_connect:activities:page:0", {"source_ref": str(latest.id)})
    state(
        "activity-version:synthetic-old-summary", {"requested_at": summary.fetched_at.isoformat()}
    )

    detail = raw(9, "activity_details", "synthetic-old-summary", payload=[{"synthetic": True}])
    session.add(
        ActivityPart(
            activity_id=detail.source_key,
            kind=detail.endpoint,
            sequence=0,
            payload={"synthetic": True},
        )
    )
    state(
        f"ingest:garmin_connect:{detail.endpoint}:{detail.source_key}",
        {"source_ref": str(latest.id)},
    )
    raw(10, "activity_weather", "synthetic-old-summary")
    state("ingest:garmin_connect:activity_weather:synthetic-old-summary", {})

    reading = raw(11, "heart_rate", SCOPE)
    state(f"ingest:garmin_connect:heart_rate:{SCOPE}", {"source_ref": str(latest.id)})
    state("sample-owner:synthetic", {"source_ref": str(reading.id)})
    raw(12, "heart_rate", SCOPE, version=9)
    raw(13, "heart_rate", SCOPE, source="garmin_personal")
    for number in range(14, 157):
        raw(number, "hydration", f"synthetic-day-{number}")
    session.flush()


def run(binary):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            seed(session)
        with Session(engine) as session, session.begin():

            def expected(target):
                with patch.object(replay, "PARSER_VERSION", target):
                    return session.execute(
                        select(cast(SourcePayload.id, String), replay.canonical_source())
                        .where(
                            SourcePayload.source == "garmin_connect",
                            SourcePayload.parser_version != target,
                        )
                        .order_by(SourcePayload.id)
                    ).all()

            upgrade, rollback = expected(9), expected(8)
            assert len(upgrade) > 128
            assert len(rollback) == 1
            encoded = json.dumps(
                {
                    "upgrade": [list(row) for row in upgrade],
                    "rollback": [list(row) for row in rollback],
                }
            )
        subprocess.run(
            [binary, "--ignored"],
            check=True,
            env={**os.environ, "GA_TEST_EXPECTED_REPLAY": encoded},
        )
        print(f"Matched {len(upgrade)} upgrade and {len(rollback)} rollback eligibility decisions")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
