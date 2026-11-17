"""Capture full stored sleep analysis from synthetic nights and nap reports."""

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.models import Event, HealthDay, TimelineInterval
from garmin_ai.sleep_analysis import sleep_analysis


def stamp(text):
    return datetime.fromisoformat(text + "+00:00")


def seed(session):
    days = [
        (date(2026, 10, 24), 25200, 5400, 6000, 13800, 88),
        (date(2026, 10, 25), 25200, 1200, 1000, 1300, 73),
        (date(2026, 10, 26), 24000, 5400, 6000, 12600, None),
        (date(2026, 10, 27), 23000, None, 5000, 9600, 87),
        (date(2026, 10, 29), 0, 0, 0, 0, 0),
    ]
    for day, duration, deep, rem, light, score in days:
        session.add(
            HealthDay(
                day=day,
                sleep_seconds=duration,
                deep_seconds=deep,
                rem_seconds=rem,
                light_seconds=light,
                sleep_score=score,
                sources={"field:sleep_seconds": "synthetic", "field:rem_seconds": "синтетический"},
            )
        )
    for day, start, end in [
        ("2026-10-24", "2026-10-23T22:00:00", "2026-10-24T05:00:00"),
        ("2026-10-25", "2026-10-24T22:15:00", "2026-10-25T05:15:00"),
        ("2026-10-26", "2026-10-25T22:30:00", "2026-10-26T06:30:00"),
        ("2026-10-29", "2026-10-28T22:00:00", "2026-10-29T06:30:00"),
    ]:
        session.add(
            TimelineInterval(
                id=f"sleep:{day}",
                start=stamp(start),
                end=stamp(end),
                label="sleep",
                source="garmin_connect",
                confidence=1,
                confirmed=True,
                evidence={"source_ref": day},
            )
        )

    entries = [
        ("2026-10-24T12:00:00", "2026-10-24T13:00:00", "Europe/Bratislava", True, "manual"),
        ("2026-10-24T12:20:00", "2026-10-24T13:20:00", "Europe/Bratislava", True, "manual"),
        ("2026-10-25T05:00:00", "2026-10-25T05:20:00", "Europe/Bratislava", True, "manual"),
        ("2026-10-26T13:00:00", None, "Europe/Bratislava", True, "manual"),
        ("2026-10-27T12:00:00", "2026-10-27T12:10:00", "Europe/Bratislava", True, "manual"),
        ("2026-10-27T23:45:00", "2026-10-28T00:05:00", "Asia/Tokyo", True, "manual"),
        ("2026-10-26T13:00:00", "2026-10-26T13:10:00", "Europe/Bratislava", True, "inferred"),
        ("2026-10-26T13:00:00", "2026-10-26T13:10:00", "Europe/Bratislava", False, "manual"),
        ("2026-10-29T13:00:00", "2026-10-31T13:00:00", "Europe/Bratislava", True, "manual"),
    ]
    entries += [
        (f"2026-10-24T15:{index:02}:00", f"2026-10-24T15:{index + 1:02}:00", "UTC", True, "manual")
        for index in range(12)
    ]
    for index, (start, end, zone, confirmed, source) in enumerate(entries, 1):
        session.add(
            Event(
                id=UUID(int=index),
                kind="nap",
                start=stamp(start),
                end=stamp(end) if end else None,
                timezone=zone,
                source=source,
                status="confirmed" if confirmed else "pending",
                payload={"type": "nap"},
            )
        )
    session.commit()


def run(output):
    engine = isolated_engine()
    try:
        with Session(engine) as session:
            seed(session)
            results = []
            for start, end, zone, policy in [
                ("2026-10-24", "2026-10-29", "Europe/Bratislava", "separate"),
                ("2026-10-24", "2026-10-29", "Europe/Bratislava", "include_confirmed"),
                ("2026-10-11", "2026-11-10", "Europe/Bratislava", "separate"),
                ("2026-10-24", "2026-10-29", "Pacific/Kiritimati", "include_confirmed"),
                ("2026-10-30", "2026-10-30", "UTC", "separate"),
                ("2026-10-24", "2026-10-24", "UTC", "include_confirmed"),
                ("2026-10-01", "2026-11-01", "UTC", "separate"),
            ]:
                try:
                    value = sleep_analysis(
                        session, date.fromisoformat(start), date.fromisoformat(end), zone, policy
                    )
                except ValueError:
                    value = "invalid"
                results.append(
                    {"start": start, "end": end, "zone": zone, "policy": policy, "value": value}
                )
            output.write_text(json.dumps(results, ensure_ascii=False))
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)


if __name__ == "__main__":
    main()
