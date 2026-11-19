"""Compare historical clocks, repeated hours and temporal bounds with Python."""

import argparse
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from check_temporal_features import (
    EVENT,
    FEATURE_VERSION,
    METRICS,
    SOURCE,
    compare_cases,
    large_context,
    seed,
)
from sqlalchemy import literal, text
from sqlalchemy.orm import Session

from garmin_ai.models import MetricObservation


def seed_clocks(engine):
    with Session(engine) as session, session.begin():
        for index, clock in enumerate(
            [
                "2026-11-01T04:30:00Z",
                "2026-11-01T05:15:00Z",
                "2026-11-01T05:30:00Z",
                "2026-11-01T05:45:00Z",
                "2026-11-01T06:15:00Z",
                "2026-11-01T06:30:00Z",
                "2026-11-01T06:45:00Z",
                "2026-11-01T07:30:00Z",
            ]
        ):
            at = datetime.fromisoformat(clock)
            session.add(
                MetricObservation(
                    id=UUID(int=8100000 + index),
                    source_ref=SOURCE,
                    metric="synthetic_fold",
                    unit="count",
                    value=index + 1.0,
                    source_calendar_date=at.date(),
                    observed_at=at,
                    effective_start=at - timedelta(minutes=10),
                    fetched_at=at - timedelta(days=1),
                    ingested_at=at - timedelta(days=1),
                    timezone="America/New_York",
                    quality="observed",
                    sequence=index,
                    feature_version=FEATURE_VERSION,
                    valid=True,
                )
            )


def cases():
    values = []
    for offset in [0, 345, -240, 840, -720]:
        zone = timezone(timedelta(minutes=offset))
        for shift in [-1, 0, 1]:
            event = (EVENT + timedelta(microseconds=shift)).astimezone(zone)
            values.append(
                (METRICS, [event], (EVENT + timedelta(hours=2)).astimezone(zone), "retrospective")
            )
            values.append((METRICS, [event], event, "as_known"))
    for first, second in [(0, 345), (345, 0), (-240, 840), (840, -240)]:
        one = EVENT.astimezone(timezone(timedelta(minutes=first)))
        two = EVENT.astimezone(timezone(timedelta(minutes=second)))
        values.append((METRICS, [one, two], EVENT, "as_known"))
        values.append(((), [one, two], EVENT, "as_known"))
    for minute in range(15, 241, 5):
        event = datetime(2026, 11, 1, 4, tzinfo=UTC) + timedelta(minutes=minute)
        values.append(
            (("synthetic_fold",), [event], datetime(2026, 11, 2, tzinfo=UTC), "retrospective")
        )
    for events in [
        [],
        [datetime(1, 1, 1, tzinfo=UTC)],
        [datetime(1, 1, 2, tzinfo=UTC)],
        [datetime.fromisoformat("0001-01-02T00:00:00+23:00")],
        [datetime.fromisoformat("0001-01-01T23:00:00-00:30")],
        [datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)],
    ]:
        for metrics in [(), ("unknown",)]:
            values.append((metrics, events, EVENT, "retrospective"))
    return values


def distinct_history(engine, count):
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM metric_observations WHERE metric='synthetic_distinct'")
        )
        connection.execute(
            text("""
            INSERT INTO metric_observations(
                id,source_ref,metric,unit,value,source_calendar_date,observed_at,
                fetched_at,ingested_at,timezone,quality,sequence,feature_version,valid
            )
            SELECT gen_random_uuid(),:source,'synthetic_distinct','test',n,
                DATE '2026-10-25',CAST(:origin AS timestamptz)+n*interval '1 microsecond',
                :origin,:origin,'UTC','observed',n,:version,true
            FROM generate_series(1,:count) AS n
            """),
            {
                "source": SOURCE,
                "origin": EVENT - timedelta(hours=2),
                "version": FEATURE_VERSION,
                "count": count,
            },
        )


def configure_zone(engine, zone, role=False):
    database = engine.dialect.identifier_preparer.quote(engine.url.database)
    zone = literal(zone).compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if role:
            connection.execute(
                text(f"ALTER ROLE CURRENT_USER IN DATABASE {database} SET TimeZone={zone}")
            )
        else:
            connection.execute(text(f"ALTER DATABASE {database} SET TimeZone={zone}"))
    engine.dispose()


def run(binary):
    engine = isolated_engine()
    database = engine.dialect.identifier_preparer.quote(engine.url.database)
    try:
        seed(engine)
        large_context(engine)
        seed_clocks(engine)
        requests = cases()
        for zone in [
            "UTC",
            "America/New_York",
            "Asia/Kathmandu",
            "GMT+3",
            "GMT-3",
            "<+0545>-5:45",
            "EST5EDT",
            "Etc/GMT+3",
        ]:
            configure_zone(engine, zone)
            compare_cases(binary, engine, requests, "database timezone " + zone)
        configure_zone(engine, "America/New_York")
        configure_zone(engine, "Asia/Kathmandu", role=True)
        compare_cases(binary, engine, requests, "database/role precedence")
        configure_zone(engine, "GMT+3", role=True)
        compare_cases(binary, engine, requests, "database/role POSIX precedence")
        configure_zone(engine, "UTC", role=True)
        for count in [1000, 10000, 100000]:
            distinct_history(engine, count)
            clocks = [
                EVENT
                - timedelta(hours=2)
                + timedelta(microseconds=i * max(count, 1999) // 1999 + 1)
                for i in range(1999)
            ] + [EVENT]
            results = compare_cases(
                binary,
                engine,
                [(("synthetic_distinct",), clocks, EVENT - timedelta(hours=2), "as_known")],
                f"{count} distinct source times with 2000 event clocks",
            )
            assert isinstance(results[0], list) and len(results[0]) == 2000
            assert results[0][-1][1]["synthetic_distinct"]["value"] == count
    finally:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text(f"ALTER ROLE CURRENT_USER IN DATABASE {database} RESET TimeZone")
            )
            connection.execute(text(f"ALTER DATABASE {database} RESET TimeZone"))
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
