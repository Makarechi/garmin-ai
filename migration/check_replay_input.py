"""Capture read-only replay decisions from the pinned Python application."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai import replay
from garmin_ai.archive import LocalArchive
from garmin_ai.models import Activity, AppState, HealthDay, MetricObservation, SourcePayload

AT = datetime(2026, 10, 25, 0, 55, tzinfo=UTC)
ACCOUNT = "a" * 64
CONFIGURED_ZONE = "America/New_York"


def at(minutes):
    return AT + timedelta(minutes=minutes)


def parsed_json(endpoint, key, payload, timezone, **kwargs):
    replacement = kwargs["replacement"]
    if replacement is not None:
        replacement.validate(endpoint)
    return {
        "status": "json",
        "endpoint": endpoint,
        "key": key,
        "payload": payload,
        "timezone": timezone,
        "requested_at": kwargs["fetched_at"].astimezone(UTC).isoformat(),
        "replacement": replacement.serialize() if replacement else None,
    }


def parsed_fit(_session, _archive, key, payload, *, fetched_at, replay):
    assert replay
    return {
        "status": "fit",
        "key": key,
        "bytes": list(payload),
        "requested_at": fetched_at.astimezone(UTC).isoformat(),
    }


def seed(session, archive):
    def state(key, value):
        session.add(AppState(key=key, value=value))

    def raw(number, endpoint, key, payload, *, version=8, status="normalized", **kwargs):
        source = kwargs.get("source", "garmin_connect")
        path = (
            archive.put_bytes(payload, "fit")
            if endpoint == "activity_fit"
            else archive.put_json(payload)
        )
        row = SourcePayload(
            id=UUID(int=number),
            source=source,
            endpoint=endpoint,
            source_key=key,
            status=status,
            parser_version=version,
            fetched_at=at(0),
            payload=None if endpoint == "activity_fit" else payload,
            payload_hash=kwargs.get("digest", path.split("/")[1].split(".")[0]),
            archive_key=path,
        )
        session.add(row)
        return row

    def clock(row, minute, **values):
        return {"source_ref": str(row.id), "requested_at": at(minute).isoformat(), **values}

    def version(row, entry):
        prefix = (
            "fit-version:"
            if row.endpoint == "activity_fit"
            else (f"ingest:garmin_connect:{row.endpoint}:")
        )
        state(prefix + row.source_key, entry)

    def metadata(row, **values):
        state(f"ingest-meta:{row.id}", values)

    def observe(row, timezone, minute, sequence=0):
        session.add(
            MetricObservation(
                metric="heart_rate_bpm",
                value=70,
                unit="bpm",
                source_calendar_date=AT.date(),
                source_ref=row.id,
                fetched_at=at(minute),
                timezone=timezone,
                quality="observed",
                sequence=sequence,
                feature_version="synthetic",
            )
        )

    steps = [{"startGMT": "2026-10-25 01:05:00", "steps": 40}]
    newer_steps = [{**steps[0], "steps": 47}]
    attestation = dict(
        start=at(4).isoformat(),
        end=at(79).isoformat(),
        metrics=["steps_bucket"],
        evidence="synthetic complete interval",
    )

    state("account:garmin", {"fingerprint": ACCOUNT})
    retained_day = raw(
        1, "daily", "2026-10-22", {"totalSteps": 111, "syntheticLargeInteger": 2**170 + 1}
    )
    newer_day = raw(2, "daily", "2026-10-22", {"totalSteps": 222})
    version(newer_day, clock(newer_day, 40))
    session.add(
        HealthDay(
            day=date(2026, 10, 22),
            steps=111,
            sources={"field:steps": str(retained_day.id), "time:steps": at(33).isoformat()},
        )
    )
    current = raw(3, "steps", "2026-10-23", steps, version=9)
    version(current, clock(current, 5, replacement=attestation))
    metadata(current, timezone="Europe/Bratislava", applied_at=at(25).isoformat())

    failed = raw(4, "steps", "2026-10-24", steps, status="error")
    prior = raw(5, "steps", "2026-10-24", newer_steps)
    version(prior, clock(prior, 5, latest_attempt=clock(failed, 21, replacement=attestation)))
    metadata(failed, timezone="Europe/Bratislava", applied_at=at(45).isoformat())

    retained = raw(6, "steps", "2026-10-25", steps)
    latest = raw(7, "steps", "2026-10-25", newer_steps)
    version(latest, clock(latest, 50))
    metadata(retained, timezone="Europe/Prague", applied_at=at(72).isoformat())
    state(
        "ingest-history:garmin_connect:steps:2026-10-25",
        {
            "applications": [
                {"raw_ref": str(retained.id), "at": at(12).isoformat(), "timezone": "UTC"},
                {
                    "raw_ref": str(retained.id),
                    "at": at(35).isoformat(),
                    "timezone": "Europe/Prague",
                    "replacement": attestation,
                },
            ]
        },
    )
    state("sample-owner:synthetic-recorded-order", {"source_ref": str(retained.id)})

    old = raw(8, "steps", "2026-10-26", steps)
    new = raw(9, "steps", "2026-10-26", newer_steps)
    version(new, clock(new, 52))
    metadata(old, timezone="Europe/Prague")
    state("sample-owner:synthetic-unknown-order", {"source_ref": str(old.id)})

    raw(10, "steps", "2026-10-27", steps)
    raw(11, "steps", "2026-10-28", [], status="empty")
    raw(12, "activities", "page:0", [{"activityId": 99}])
    raw(13, "activities", "page:1", [{"activityId": 98}])
    raw(14, "heart_rate", "2026-10-25", {}, source="garmin_personal")
    raw(15, "daily", "2026-10-22", {"totalSteps": 999})
    history = raw(16, "heart_rate", "2026-10-29", {"heartRateValues": [[1793490000000, 70]]})
    observe(history, "Europe/Sofia", 14)
    observe(history, "Europe/Madrid", 51)
    observe(history, "Europe/Madrid", 51, sequence=1)

    fit = raw(17, "activity_fit", "synthetic-fit-replay", b"synthetic retained FIT")
    same_fit = raw(18, "activity_fit", "synthetic-fit-current", b"synthetic current FIT", version=9)
    corrupt = raw(19, "activity_fit", "synthetic-fit-corrupt", b"synthetic corrupt FIT", version=9)
    damaged = archive.root / corrupt.archive_key
    damaged.write_bytes(b"S" + damaged.read_bytes()[1:])
    version(fit, clock(fit, 66, latest_attempt=clock(fit, 70)))
    raw(20, "daily", "2026-10-30", {}, digest="0" * 64)
    broken_replacement = raw(21, "steps", "2026-10-31", steps)
    version(
        broken_replacement,
        clock(broken_replacement, 0, replacement={**attestation, "metrics": ["stress_score"]}),
    )
    metadata(broken_replacement, timezone="Europe/Prague")

    for identity, zone, key in [
        ("99", "Europe/Prague", None),
        (fit.source_key, "UTC", fit.archive_key),
        (same_fit.source_key, "UTC", same_fit.archive_key),
        (corrupt.source_key, "UTC", corrupt.archive_key),
    ]:
        session.add(
            Activity(id=identity, kind="synthetic", start=AT, end=AT, timezone=zone, fit_key=key)
        )
    raw(22, "activity", "99", {"activityId": 99})
    ambiguous = raw(23, "heart_rate", "2026-11-01", {"heartRateValues": [[1793490000000, 70]]})
    observe(ambiguous, "Europe/Sofia", 51)
    observe(ambiguous, "Europe/Madrid", 51, sequence=1)


def capture(archive, output):
    engine = isolated_engine()
    try:
        with Session(engine) as session, session.begin():
            seed(session, archive)
        cases = []
        with (
            Session(engine) as session,
            session.begin(),
            patch.object(
                replay,
                "ingest",
                lambda _session, _archive, *args, **kwargs: parsed_json(*args, **kwargs),
            ),
            patch.object(replay, "store_fit", parsed_fit),
            patch.object(replay, "invalidate_outputs"),
        ):
            for number, target in [*((i, 9) for i in range(1, 25)), (3, 8), (3, 10)]:
                identity = str(UUID(int=number))
                try:
                    expected = replay.replay_source(
                        session,
                        archive,
                        SimpleNamespace(timezone=CONFIGURED_ZONE),
                        {"raw_ref": identity, "target_version": target},
                    )
                except ValueError as error:
                    expected = {"error": str(error)}
                cases.append({"source": identity, "target": target, "expected": expected})
        assert any("error" in case["expected"] for case in cases)
        output.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        print(f"Captured {len(cases)} offline replay input decisions")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    capture(LocalArchive(args.archive), args.output)
