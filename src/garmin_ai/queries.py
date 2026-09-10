import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Float, Integer, cast, func, select, tuple_

from garmin_ai.config import Settings
from garmin_ai.events import (
    OPEN_EPISODE_KINDS,
    EventInput,
    event_overlap,
    serialize,
    serialize_event,
)
from garmin_ai.freshness import observation_freshness, source_metadata
from garmin_ai.metrics import CATALOG, contract
from garmin_ai.models import (
    Activity,
    ActivityPart,
    AppState,
    Event,
    HealthDay,
    Insight,
    TimelineInterval,
)


def date_range(start: date, end: date, maximum=3660):
    if start < date.min + timedelta(days=60) or end > date.max - timedelta(days=60):
        raise ValueError("Dates must allow bounded analysis-window expansion")
    if not 0 <= (end - start).days <= maximum:
        raise ValueError(f"Date range must be ordered and at most {maximum} days")


def time_range(start: datetime, end: datetime, maximum=366):
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Timezone-aware timestamps required")
    try:
        start, end = start.astimezone(UTC), end.astimezone(UTC)
    except OverflowError:
        raise ValueError("Timestamp outside supported calendar") from None
    date_range(start.date(), end.date(), maximum + 1)
    if not timedelta(0) < end - start <= timedelta(days=maximum):
        raise ValueError("Timestamp range must be ordered and bounded")


HEALTH_METRICS = {
    c.name for c in HealthDay.__table__.columns if isinstance(c.type, (Float, Integer))
}


SNAPSHOT_FIELDS = HEALTH_METRICS | {"hrv_status", "training_status"}
MEASUREMENT_METRICS = {name for name, spec in CATALOG.items() if spec.kind != "daily_summary"}


def health_range(session, start: date, end: date):
    date_range(start, end)
    rows = session.scalars(
        select(HealthDay).where(HealthDay.day.between(start, end)).order_by(HealthDay.day)
    ).all()
    return {
        "start": str(start),
        "end": str(end),
        "days_available": len(rows),
        "days_requested": (end - start).days + 1,
        "rows": [serialize(r) for r in rows],
    }


def health_snapshot(session, day: date):
    row = session.get(HealthDay, day)
    values = serialize(row) if row else None
    return {
        "date": str(day),
        "available": row is not None,
        "values": values,
        "time_semantics": "daily_summary; updated_at is ingestion time, not measurement time",
        "usable_for_current_state": False,
        "metric_contracts": {name: contract(name) for name in CATALOG},
        "hydration_sources": "Garmin daily total; manual diary water is separate and not summed",
        "missing_metrics": sorted(
            k for k in SNAPSHOT_FIELDS if row is None or getattr(row, k) is None
        ),
    }


def metric_series(
    session, metric: str, start: datetime, end: datetime, minutes: int = 5, limit: int = 2000
):
    time_range(start, end)
    if metric not in MEASUREMENT_METRICS:
        raise ValueError("Unknown measurement metric")
    if not 1 <= minutes <= 1440 or not 1 <= limit <= 5000:
        raise ValueError("Invalid series resolution or limit")
    from garmin_ai.metric_series import series

    return series(session, metric, start, end, minutes, limit)


def list_activities(session, start: datetime, end: datetime, kind: str | None = None, limit=200):
    time_range(start, end, 3660)
    if not 1 <= limit <= 1000:
        raise ValueError("Invalid activity limit")
    query = select(Activity).where(Activity.start >= start, Activity.start < end)
    if kind:
        query = query.where(Activity.kind == kind)
    rows = session.scalars(query.order_by(Activity.start).limit(limit + 1)).all()
    return {"rows": [serialize(r) for r in rows[:limit]], "truncated": len(rows) > limit}


def activity_details(
    session, activity_id: str, include_samples=False, offset: int = 0, limit: int = 100
):
    if not 0 <= offset <= 1000000 or not 1 <= limit <= 2000:
        raise ValueError("Invalid activity pagination")
    row = session.get(Activity, activity_id)
    if not row:
        raise LookupError("Activity not found")
    query = select(ActivityPart).where(ActivityPart.activity_id == activity_id)
    if not include_samples:
        query = query.where(ActivityPart.kind.not_in(["fit_record", "activity_details"]))
    parts = session.scalars(
        query.order_by(ActivityPart.kind, ActivityPart.sequence).offset(offset).limit(limit + 1)
    ).all()
    return {
        "activity": serialize(row),
        "parts": [serialize(p) for p in parts[:limit]],
        "truncated": len(parts) > limit,
        "next_offset": offset + limit if len(parts) > limit else None,
    }


EVENT_KINDS = frozenset(
    EventInput.model_json_schema()["properties"]["payload"]["discriminator"]["mapping"]
)


def list_events(session, start: datetime, end: datetime, kind: str | None = None, limit=500):
    time_range(start, end, 3660)
    if kind is not None and kind not in EVENT_KINDS:
        raise ValueError("Unknown event kind")
    if not 1 <= limit <= 1000:
        raise ValueError("Invalid event limit")
    query = select(Event).where(
        Event.deleted.is_(False),
        event_overlap(start, end),
    )
    if kind:
        query = query.where(Event.kind == kind)
    rows = session.scalars(
        query.order_by((Event.start >= start).desc(), Event.start, Event.id).limit(limit + 1)
    ).all()
    return {"rows": [serialize_event(r) for r in rows[:limit]], "truncated": len(rows) > limit}


def timeline(session, start: datetime, end: datetime):
    time_range(start, end, 31)

    def bounded(statement):
        rows = session.scalars(statement.limit(501)).all()
        if len(rows) > 500:
            raise ValueError("Timeline exceeds 500 annotations; narrow the interval")
        return rows

    candidates = []
    for a in bounded(select(Activity).where(Activity.start < end, Activity.end > start)):
        candidates.append(
            dict(
                start=max(start, a.start),
                end=min(end, a.end),
                label=a.kind,
                confidence=1,
                status="known",
                source="garmin_activity",
                layer="activity",
                evidence={"activity_id": a.id},
                priority=3,
            )
        )
    for i in bounded(
        select(TimelineInterval).where(TimelineInterval.start < end, TimelineInterval.end > start)
    ):
        candidates.append(
            dict(
                start=max(start, i.start),
                end=min(end, i.end),
                label=i.label,
                confidence=i.confidence,
                status="planned"
                if i.source in {"calendar", "external_calendar"}
                else "known"
                if i.confirmed
                else "inferred",
                source=i.source,
                layer=(
                    "plans"
                    if i.source in {"calendar", "external_calendar"}
                    else "sleep"
                    if i.label in {"sleep", "nap"}
                    else "context"
                ),
                evidence=i.evidence,
                priority=2 if i.confirmed and i.source != "garmin_connect" else 1,
            )
        )
    for e in bounded(
        select(Event).where(
            Event.deleted.is_(False),
            event_overlap(start, end),
        )
    ):
        candidates.append(
            dict(
                start=max(start, e.start),
                end=min(end, e.end)
                if e.end
                else (end if e.kind in OPEN_EPISODE_KINDS else e.start),
                label=e.payload.get("description", e.kind),
                confidence=e.confidence,
                status="known" if e.status == "confirmed" else e.status,
                source=e.source,
                layer="sleep"
                if e.kind == "nap"
                else "wellbeing"
                if e.kind in {"migraine", "illness", "medication", "mood", "headache_observation"}
                else "context",
                topology=serialize_event(e)["topology"],
                evidence={"event_id": str(e.id)},
                priority=2 if e.status == "confirmed" else 0,
            )
        )
    if len(candidates) > 500:
        raise ValueError("Timeline exceeds 500 annotations; narrow the interval")
    # Preserve independent annotations, including point events, rather than
    # flattening every overlap into the legacy display label.
    layers = {name: [] for name in ("sleep", "activity", "wellbeing", "context", "plans")}
    candidates.sort(
        key=lambda c: (
            c["layer"],
            c["start"],
            c["end"],
            c["source"],
            json.dumps(c["evidence"], sort_keys=True),
        )
    )
    for index, candidate in enumerate(candidates):
        candidate["annotation_id"] = index
        layers[candidate["layer"]].append(
            {
                **{k: v for k, v in candidate.items() if k not in {"priority", "start", "end"}},
                "start": candidate["start"].isoformat(),
                "end": candidate["end"].isoformat(),
            }
        )
    boundaries = sorted({start, end} | {c[k] for c in candidates for k in ("start", "end")})
    segments = []
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        matches = [c for c in candidates if c["start"] <= left and c["end"] >= right]
        if matches:
            matches.sort(
                key=lambda c: (
                    c["priority"],
                    c["confidence"],
                    c["source"],
                    json.dumps(c["evidence"], sort_keys=True),
                )
            )
            best = matches[-1]
            values = {k: v for k, v in best.items() if k not in {"start", "end", "priority"}}
            values["overlapping_evidence"] = [c["evidence"] for c in matches if c is not best]
        else:
            values = dict(label="unknown", status="unknown", confidence=0, source=None, evidence={})
        segments.append(
            dict(
                start=left.isoformat(),
                end=right.isoformat(),
                annotations=[c["annotation_id"] for c in matches],
                **values,
            )
        )
    return {
        "segments": segments,
        "layers": layers,
        "events": list_events(session, start, end),
        "semantics": "Layers preserve overlapping evidence; segment label is a legacy display projection. Calendar entries are plans, not attendance. Open symptoms have no recorded end, not confirmed persistence.",
    }


def latest_freshness_rows(session, today):
    endpoint = func.split_part(AppState.key, ":", 2)
    key = AppState.value["source_key"].as_string()
    historical = (key.op("~")(r"^\d{4}-\d{2}-\d{2}$") & (key != str(today))).is_(True)
    fetched = func.coalesce(
        AppState.value["fetched_at"].as_string(), AppState.value["success_at"].as_string()
    )
    ranked = (
        select(
            AppState.key,
            AppState.value,
            historical.label("historical"),
            func.row_number()
            .over(
                partition_by=(endpoint, historical),
                order_by=(cast(fetched, DateTime(timezone=True)).desc(), AppState.key),
            )
            .label("rank"),
        )
        .where(AppState.key.startswith("freshness:"), fetched.is_not(None))
        .subquery()
    )
    return session.execute(
        select(ranked.c.key, ranked.c.value, ranked.c.historical).where(ranked.c.rank == 1)
    ).all()


def data_freshness(session, now=None):
    now = now or datetime.now(UTC)
    timezone = session.info.get("timezone") or Settings().timezone
    today = now.astimezone(ZoneInfo(timezone)).date()
    rows = latest_freshness_rows(session, today)
    endpoints = {}
    historical = {}
    for row in rows:
        endpoint = row.key.split(":", 2)[1]
        value = dict(row.value)
        target = historical if row.historical else endpoints
        fetched = value.get("fetched_at") or value.get("success_at")
        if fetched and (endpoint not in target or fetched > target[endpoint]["fetched_at"]):
            success = value.get("success_at")
            value["lag_seconds"] = (
                max(0, (now - datetime.fromisoformat(success)).total_seconds()) if success else None
            )
            value["fetched_at"] = fetched
            value["last_success_at"] = success
            value["fetch_lag_seconds"] = max(
                0, (now - datetime.fromisoformat(fetched)).total_seconds()
            )
            target[endpoint] = value
    for value in [*endpoints.values(), *historical.values()]:
        value.update(source_metadata(session, value.get("source_ref")))
    return {
        "checked_at": now.isoformat(),
        "endpoints": endpoints,
        "historical": historical,
        "available": bool(endpoints),
        "channels": observation_freshness(session, now, timezone, endpoints),
        "limitations": [
            "Fetch success does not establish fresh observations or complete device wear",
            "Coverage joins adjacent valid samples only; gaps are never filled",
            "Daily summaries have calendar semantics, not an invented measurement timestamp",
        ],
    }


def insights_list(session, limit=30, cursor: str | None = None):
    if not 1 <= limit <= 100:
        raise ValueError("Invalid insight limit")
    query = select(Insight)
    if cursor is not None:
        try:
            if len(cursor) > 200:
                raise ValueError("Cursor too long")
            timestamp, identity = cursor.split("|", 1)
            before = datetime.fromisoformat(timestamp)
            if before.tzinfo is None:
                raise ValueError("Cursor timestamp requires timezone")
            query = query.where(
                tuple_(Insight.generated_at, Insight.id) < tuple_(before, UUID(identity))
            )
        except (ValueError, TypeError):
            raise ValueError("Invalid insight cursor") from None
    rows = session.scalars(
        query.order_by(Insight.generated_at.desc(), Insight.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    more = len(rows) > limit
    return {
        "rows": [serialize(row) for row in page],
        "truncated": more,
        "next_cursor": f"{page[-1].generated_at.isoformat()}|{page[-1].id}" if more else None,
    }
