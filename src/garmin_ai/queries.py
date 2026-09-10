import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Float, Integer, func, or_, select, tuple_

from garmin_ai.config import Settings
from garmin_ai.events import EventInput, serialize
from garmin_ai.models import (
    Activity,
    ActivityPart,
    AppState,
    Event,
    HealthDay,
    Insight,
    Measurement,
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
MEASUREMENT_METRICS = {
    "heart_rate_bpm",
    "stress_score",
    "body_battery",
    "spo2_pct",
    "respiration_rpm",
    "steps_bucket",
    "hrv_rmssd_ms",
    "hydration_ml",
}


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
    bucket = func.time_bucket(timedelta(minutes=minutes), Measurement.ts).label("bucket")
    rows = session.execute(
        select(
            bucket,
            func.avg(Measurement.value),
            func.min(Measurement.value),
            func.max(Measurement.value),
            func.count(),
            Measurement.unit,
        )
        .where(Measurement.metric == metric, Measurement.ts >= start, Measurement.ts < end)
        .group_by(bucket, Measurement.unit)
        .order_by(bucket)
        .limit(limit + 1)
    ).all()
    return {
        "metric": metric,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bucket_minutes": minutes,
        "truncated": len(rows) > limit,
        "rows": [
            {
                "ts": r[0].isoformat(),
                "mean": r[1],
                "min": r[2],
                "max": r[3],
                "samples": r[4],
                "unit": r[5],
            }
            for r in rows[:limit]
        ],
    }


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
        # Unknown FIT families can contain sample arrays. Keep only explicit
        # summary/metadata families by default; all parts remain opt-in.
        query = query.where(
            ActivityPart.kind != "activity_details",
            or_(
                ~ActivityPart.kind.startswith("fit_"),
                ActivityPart.kind.in_(
                    [
                        "fit_activity",
                        "fit_session",
                        "fit_lap",
                        "fit_file_id",
                        "fit_file_creator",
                        "fit_device_info",
                        "fit_sport",
                        "fit_zones_target",
                        "fit_user_profile",
                        "fit_developer_data_id",
                        "fit_field_description",
                    ]
                ),
            ),
        )
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
        Event.start < end,
        or_(
            Event.end > start,
            ((Event.end.is_(None) | (Event.end == Event.start)) & (Event.start >= start)),
        ),
    )
    if kind:
        query = query.where(Event.kind == kind)
    rows = session.scalars(query.order_by(Event.start).limit(limit + 1)).all()
    return {"rows": [serialize(r) for r in rows[:limit]], "truncated": len(rows) > limit}


def timeline(session, start: datetime, end: datetime):
    time_range(start, end, 31)
    candidates = []
    for a in session.scalars(select(Activity).where(Activity.start < end, Activity.end > start)):
        candidates.append(
            dict(
                start=max(start, a.start),
                end=min(end, a.end),
                label=a.kind,
                confidence=1,
                status="known",
                source="garmin_activity",
                evidence={"activity_id": a.id},
                priority=3,
            )
        )
    for i in session.scalars(
        select(TimelineInterval).where(TimelineInterval.start < end, TimelineInterval.end > start)
    ):
        candidates.append(
            dict(
                start=max(start, i.start),
                end=min(end, i.end),
                label=i.label,
                confidence=i.confidence,
                status="known" if i.confirmed else "inferred",
                source=i.source,
                evidence=i.evidence,
                priority=2 if i.confirmed and i.source != "garmin_connect" else 1,
            )
        )
    for e in session.scalars(
        select(Event).where(
            Event.deleted.is_(False),
            Event.kind.in_(["context", "nap", "travel"]),
            Event.start < end,
            Event.end > start,
        )
    ):
        candidates.append(
            dict(
                start=max(start, e.start),
                end=min(end, e.end),
                label=e.payload.get("description", e.kind),
                confidence=e.confidence,
                status="known" if e.status == "confirmed" else "inferred",
                source=e.source,
                evidence={"event_id": str(e.id)},
                priority=2 if e.status == "confirmed" else 0,
            )
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
        segments.append(dict(start=left.isoformat(), end=right.isoformat(), **values))
    return {"segments": segments, "events": list_events(session, start, end)}


def data_freshness(session):
    from garmin_ai.backfill import history_status

    connection = session.get(AppState, "integration:garmin", populate_existing=True)

    now = datetime.now(UTC)
    rows = session.scalars(select(AppState).where(AppState.key.startswith("freshness:"))).all()
    today = now.astimezone(ZoneInfo(session.info.get("timezone") or Settings().timezone)).date()
    endpoints = {}
    historical = {}
    for row in rows:
        endpoint = row.key.split(":", 2)[1]
        value = dict(row.value)
        try:
            source_day = date.fromisoformat(value.get("source_key", ""))
        except ValueError:
            source_day = None
        target = historical if source_day is not None and source_day != today else endpoints
        if endpoint not in target or value["success_at"] > target[endpoint]["success_at"]:
            value["lag_seconds"] = max(
                0, (now - datetime.fromisoformat(value["success_at"])).total_seconds()
            )
            target[endpoint] = value
    return {
        "checked_at": now.isoformat(),
        "endpoints": endpoints,
        "historical": historical,
        "history_sync": history_status(session),
        "connection": connection.value if connection else {"status": "not_attempted"},
        "available": bool(endpoints),
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
