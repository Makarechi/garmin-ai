"""Generated database invariants, independent of the SQL overlap implementation."""

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from garmin_ai.events import EventInput, create_event, delete_event
from garmin_ai.models import Audit, Event
from garmin_ai.queries import list_events
from garmin_ai.tools import call_tool


@pytest.mark.regression("R01")
@pytest.mark.regression("R02")
@pytest.mark.parametrize(
    "zone,day",
    [
        ("Europe/Budapest", "2026-03-29"),
        ("Europe/Budapest", "2026-10-25"),
        ("America/New_York", "2026-03-08"),
        ("America/New_York", "2026-11-01"),
        ("Australia/Lord_Howe", "2026-04-05"),
        ("Australia/Lord_Howe", "2026-10-04"),
    ],
)
def test_interval_partition_and_instant_representation_invariants(db, zone, day):
    wall_start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(zone))
    left = wall_start.astimezone(UTC)
    right = (wall_start + timedelta(days=1)).astimezone(UTC)
    duration = int((right - left).total_seconds())
    cuts = [left + timedelta(seconds=duration * i // 4) for i in range(5)]
    rng = random.Random(280102)
    offsets = [-86400, -1, 0, 1, duration, duration + 1]
    offsets += [int((point - left).total_seconds()) for point in cuts[1:-1]]
    offsets += [rng.randrange(-86400, duration + 86400) for _ in range(24)]
    open_ids, deleted_ids = set(), set()
    generated = []
    for index, offset in enumerate(offsets):
        instant = left + timedelta(seconds=offset)
        for kind in ("caffeine", "migraine", "illness"):
            # Exercise points, zero-length points, bounded and open episodes.
            end = (
                None
                if index % 3 == 0
                else instant
                if index % 3 == 1
                else instant + timedelta(seconds=rng.randrange(1, 172800))
            )
            payload = {"type": kind}
            if kind == "caffeine":
                payload["beverage"] = "synthetic"
            if kind == "illness":
                payload["description"] = "synthetic"
            event = create_event(
                db,
                EventInput(
                    start=instant.astimezone(ZoneInfo(zone)),
                    end=end,
                    timezone=zone,
                    status=("confirmed", "inferred", "needs_confirmation")[index % 3],
                    payload=payload,
                ),
                actor="synthetic-test",
            )
            generated.append((str(event.id), instant, end, kind))
            if end is None and kind in {"migraine", "illness"} and instant < right:
                open_ids.add(str(event.id))
            if index % 7 == 0:
                delete_event(db, event.id, revision=event.revision, actor="synthetic-test")
                deleted_ids.add(str(event.id))
    db.flush()
    before = list(
        db.execute(
            select(Event.id, Event.start, Event.end, Event.revision, Event.updated_at).order_by(
                Event.id
            )
        )
    )
    audits = db.scalar(select(func.count()).select_from(Audit))

    def identities(start, end):
        result = list_events(db, start, end)
        assert not result["truncated"]
        return {item["id"] for item in result["rows"]}

    def expected(start, stop):
        active = set()
        for identity, onset, ending, kind in generated:
            if identity in deleted_ids:
                continue
            if ending is None and kind in {"migraine", "illness"}:
                included = onset < stop
            elif ending is None or ending == onset:
                included = start <= onset < stop
            else:
                included = max(start, onset) < min(stop, ending)
            if included:
                active.add(identity)
        return active

    whole = identities(left, right)
    assert whole == expected(left, right)
    for start, stop in zip(cuts, cuts[1:], strict=False):
        assert identities(start, stop) == expected(start, stop)
    partitioned = set().union(*(identities(a, b) for a, b in zip(cuts, cuts[1:], strict=False)))
    assert whole == partitioned
    assert not whole.intersection(deleted_ids)
    assert open_ids - deleted_ids <= whole
    for representation in ("UTC", zone, "Asia/Kathmandu", "Pacific/Kiritimati"):
        timezone = ZoneInfo(representation)
        represented = call_tool(
            db,
            "events",
            {
                "start": left.astimezone(timezone).isoformat(),
                "end": right.astimezone(timezone).isoformat(),
            },
        )
        assert {item["id"] for item in represented["rows"]} == whole
    # Repeated reads cannot close episodes, revise events or append mutation audits.
    db.expire_all()
    assert (
        list(
            db.execute(
                select(Event.id, Event.start, Event.end, Event.revision, Event.updated_at).order_by(
                    Event.id
                )
            )
        )
        == before
    )
    assert db.scalar(select(func.count()).select_from(Audit)) == audits
