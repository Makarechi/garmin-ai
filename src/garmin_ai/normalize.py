"""Conservative Garmin parsers. Missing/invalid upstream values never erase history."""

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.models import (
    Activity,
    ActivityPart,
    AppState,
    HealthDay,
    Measurement,
    SourcePayload,
    TimelineInterval,
)

PARSER_VERSION = 4


def timestamp(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if abs(value) > 100_000_000_000 else value, UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # Only fields explicitly documented as GMT may call this helper with a naive value.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def numeric(value, *, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if value < minimum or (maximum is not None and value > maximum):
        return None
    return value


def upsert(session, model, values, keys):
    stmt = insert(model).values(**values)
    updates = {key: getattr(stmt.excluded, key) for key in values if key not in keys}
    if "updated_at" in model.__table__.columns:
        updates["updated_at"] = func.now()
    if updates:
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=updates)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=keys)
    session.execute(stmt)


def health_fields(session, day, fields, endpoint, ref):
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    stmt = insert(HealthDay).values(
        day=day,
        **fields,
        sources={**{f"field:{field}": str(ref) for field in fields}, f"payload:{ref}": endpoint},
    )
    values = {k: getattr(stmt.excluded, k) for k in fields}
    values.update(sources=HealthDay.sources.op("||")(stmt.excluded.sources), updated_at=func.now())
    session.execute(stmt.on_conflict_do_update(index_elements=[HealthDay.day], set_=values))


def sample(
    session,
    ts,
    metric,
    value,
    unit,
    ref,
    timezone,
    *,
    maximum=None,
    minimum=0,
    source="garmin_connect",
):
    value = numeric(value, minimum=minimum, maximum=maximum)
    if value is None or ts is None:
        return
    ts = timestamp(ts)
    replaced = session.info.setdefault("replaced_metrics", set())
    marker = (str(ref), metric)
    if marker not in replaced:
        raw = session.get(SourcePayload, ref)
        if raw:
            previous = select(SourcePayload.id).where(
                SourcePayload.source == raw.source,
                SourcePayload.endpoint.in_(["stress", "body_battery"])
                if metric == "body_battery"
                else SourcePayload.endpoint == raw.endpoint,
                SourcePayload.source_key == raw.source_key,
            )
            session.execute(
                delete(Measurement).where(
                    Measurement.metric == metric,
                    Measurement.source == source,
                    Measurement.source_ref.in_(previous),
                )
            )
        replaced.add(marker)
    upsert(
        session,
        Measurement,
        dict(
            ts=ts,
            metric=metric,
            source=source,
            local_date=ts.astimezone(ZoneInfo(timezone)).date(),
            value=value,
            unit=unit,
            source_ref=ref,
        ),
        ["ts", "metric", "source"],
    )


def normalize(session, endpoint: str, key: str, payload, ref, timezone: str):
    session.info["replaced_metrics"] = set()
    try:
        return _normalize(session, endpoint, key, payload, ref, timezone)
    finally:
        session.info.pop("replaced_metrics", None)
        session.info.pop("fetch_time", None)


def _normalize(session, endpoint: str, key: str, payload, ref, timezone: str):
    if payload in (None, {}, []):
        return "empty"
    if endpoint == "activities":
        if not isinstance(payload, list):
            raise ValueError("Activity list must be an array")
        for activity in sorted(payload, key=lambda item: str(item["activityId"])):
            normalize_activity(session, activity, timezone)
        return "normalized"
    if endpoint == "activity":
        normalize_activity(session, payload, timezone)
        return "normalized"
    if endpoint.startswith("activity_"):
        if session.get(Activity, key):
            # Preserve complete detail/lap/zone documents, separate from summaries.
            parts = payload if isinstance(payload, list) else [payload]
            session.execute(
                delete(ActivityPart).where(
                    ActivityPart.activity_id == key, ActivityPart.kind == endpoint
                )
            )
            for idx, part in enumerate(parts):
                upsert(
                    session,
                    ActivityPart,
                    AppState,
                    dict(activity_id=key, kind=endpoint, sequence=idx, payload=part),
                    ["activity_id", "kind", "sequence"],
                )
        return "archived"
    try:
        day = date.fromisoformat(key)
    except ValueError:
        return "archived"
    fields = {}
    if endpoint == "daily":
        mapping = {
            "steps": "totalSteps",
            "active_calories": "activeKilocalories",
            "resting_hr": "restingHeartRate",
            "stress_avg": "averageStressLevel",
            "stress_max": "maxStressLevel",
            "body_battery_high": "bodyBatteryHighestValue",
            "body_battery_low": "bodyBatteryLowestValue",
            "body_battery_charged": "bodyBatteryChargedValue",
            "body_battery_drained": "bodyBatteryDrainedValue",
        }
        fields = {k: numeric(payload.get(v)) for k, v in mapping.items()}
        moderate, vigorous = (
            numeric(payload.get("moderateIntensityMinutes")),
            numeric(payload.get("vigorousIntensityMinutes")),
        )
        if moderate is not None and vigorous is not None:
            fields["intensity_minutes"] = moderate + 2 * vigorous
    elif endpoint == "sleep":
        dto = payload.get("dailySleepDTO") or {}
        fields = {
            k: numeric(dto.get(v))
            for k, v in {
                "sleep_seconds": "sleepTimeSeconds",
                "deep_seconds": "deepSleepSeconds",
                "rem_seconds": "remSleepSeconds",
                "light_seconds": "lightSleepSeconds",
                "awake_seconds": "awakeSleepSeconds",
            }.items()
        }
        fields["sleep_score"] = numeric(
            ((dto.get("sleepScores") or {}).get("overall") or {}).get("value"), maximum=100
        )
        start, end = dto.get("sleepStartTimestampGMT"), dto.get("sleepEndTimestampGMT")
        if start is not None and end is not None and timestamp(end) > timestamp(start):
            upsert(
                session,
                TimelineInterval,
                dict(
                    id=f"sleep:{day}",
                    start=timestamp(start),
                    end=timestamp(end),
                    label="sleep",
                    source="garmin_connect",
                    confidence=1,
                    confirmed=True,
                    evidence={"source_ref": str(ref)},
                ),
                ["id"],
            )
    elif endpoint == "hrv":
        dto = payload.get("hrvSummary") or {}
        baseline = dto.get("baseline") or {}
        fields = {
            "hrv_nightly_avg": numeric(dto.get("lastNightAvg")),
            "hrv_weekly_avg": numeric(dto.get("weeklyAvg")),
            "hrv_baseline_low": numeric(baseline.get("balancedLow")),
            "hrv_baseline_high": numeric(baseline.get("balancedUpper")),
            "hrv_status": dto.get("status"),
        }
        for reading in payload.get("hrvReadings") or []:
            sample(
                session,
                reading.get("readingTimeGMT"),
                "hrv_rmssd_ms",
                reading.get("hrvValue"),
                "ms",
                ref,
                timezone,
            )
    elif endpoint == "readiness":
        rows = payload if isinstance(payload, list) else [payload]
        rows = [r for r in rows if r.get("calendarDate", key) == key]
        if rows:
            dto = max(rows, key=lambda r: str(r.get("timestamp") or ""))
            fields = {
                "training_readiness_score": numeric(dto.get("score"), maximum=100),
                "recovery_time_minutes": 0
                if dto.get("recoveryTimeChangePhrase") == "REACHED_ZERO"
                else numeric(dto.get("recoveryTime")),
            }
    elif endpoint == "body_battery":
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if row.get("date", key) != key:
                continue
            # Stress supplies the authoritative dense body-battery stream.
            fields.update(
                body_battery_charged=numeric(row.get("charged")),
                body_battery_drained=numeric(row.get("drained")),
            )
    elif endpoint in {"heart_rate", "stress", "respiration", "spo2"}:
        array, metric, unit, minimum, maximum = {
            "heart_rate": ("heartRateValues", "heart_rate_bpm", "bpm", 1, 300),
            "stress": ("stressValuesArray", "stress_score", "score", 0, 100),
            "respiration": ("respirationValuesArray", "respiration_rpm", "rpm", 1, 100),
            "spo2": (
                "spO2HourlyAverages" if "spO2HourlyAverages" in payload else "spO2ValuesArray",
                "spo2_pct",
                "%",
                1,
                100,
            ),
        }[endpoint]
        for point in payload.get(array) or []:
            if isinstance(point, list) and len(point) >= 2:
                sample(
                    session,
                    point[0],
                    metric,
                    point[1],
                    unit,
                    ref,
                    timezone,
                    minimum=minimum,
                    maximum=maximum,
                )
        if endpoint == "stress":
            descriptors = payload.get("bodyBatteryValueDescriptorsDTOList") or []
            index = next(
                (
                    int(d["bodyBatteryValueDescriptorIndex"])
                    for d in descriptors
                    if d.get("bodyBatteryValueDescriptorKey") == "bodyBatteryLevel"
                ),
                None,
            )
            if index is not None:
                for point in payload.get("bodyBatteryValuesArray") or []:
                    if len(point) > index:
                        sample(
                            session,
                            point[0],
                            "body_battery",
                            point[index],
                            "score",
                            ref,
                            timezone,
                            maximum=100,
                        )
        if endpoint == "heart_rate":
            fields["resting_hr"] = numeric(payload.get("restingHeartRate"), minimum=1, maximum=300)
    elif endpoint == "steps":
        for bucket in payload:
            sample(
                session,
                bucket.get("startGMT"),
                "steps_bucket",
                bucket.get("steps"),
                "steps",
                ref,
                timezone,
            )
    elif endpoint == "hydration":
        sample(
            session,
            datetime.combine(day, datetime.min.time(), ZoneInfo(timezone)).isoformat(),
            "hydration_ml",
            payload.get("valueInML"),
            "ml",
            ref,
            timezone,
        )
    elif endpoint == "max_metrics":
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            fields["vo2max"] = numeric((row.get("generic") or {}).get("vo2MaxPreciseValue"))
    else:
        return "archived"
    health_fields(session, day, fields, endpoint, ref)
    return "normalized"


def normalize_activity(session, payload, timezone):
    summary = {**payload, **(payload.get("summaryDTO") or {})}
    identity = str(payload["activityId"])
    state_key = f"activity-version:{identity}"
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(state_key, 0))))
    fetched_at = session.info.get("fetch_time", datetime.now(UTC))
    state = session.get(AppState, state_key, populate_existing=True)
    if state and fetched_at < datetime.fromisoformat(state.value["requested_at"]):
        return
    start = summary.get("startTimeGMT")
    duration = numeric(summary.get("duration"))
    if not start or duration is None:
        raise ValueError("Activity lacks GMT start or duration")
    start = timestamp(start)
    elapsed = numeric(summary.get("elapsedDuration")) or duration
    fields = {
        k: numeric(summary.get(v))
        for k, v in {
            "duration_seconds": "duration",
            "moving_seconds": "movingDuration",
            "distance_m": "distance",
            "avg_hr": "averageHR",
            "max_hr": "maxHR",
            "avg_speed_mps": "averageSpeed",
            "calories": "calories",
            "cadence": "averageRunningCadenceInStepsPerMinute",
            "ascent_m": "elevationGain",
            "descent_m": "elevationLoss",
            "aerobic_effect": "aerobicTrainingEffect",
            "anaerobic_effect": "anaerobicTrainingEffect",
            "training_load": "activityTrainingLoad",
        }.items()
    }
    if fields.get("cadence") is None:
        fields["cadence"] = numeric(summary.get("averageRunCadence"))
    if fields.get("aerobic_effect") is None:
        fields["aerobic_effect"] = numeric(summary.get("trainingEffect"))
    fields = {k: v for k, v in fields.items() if v is not None}
    existing = session.get(Activity, identity)
    timezone = (payload.get("timeZoneUnitDTO") or {}).get("timeZone") or (
        existing.timezone if existing else timezone
    )
    # Validate source timezone before preserving it for activity-local analysis.
    ZoneInfo(timezone)
    kind = (payload.get("activityType") or payload.get("activityTypeDTO") or {}).get(
        "typeKey", existing.kind if existing else "unknown"
    )
    values = dict(
        id=identity,
        kind=kind,
        start=start,
        end=start + timedelta(seconds=elapsed),
        timezone=timezone,
        **fields,
    )
    if payload.get("activityName") is not None:
        values["name"] = payload["activityName"]
    upsert(session, Activity, values, ["id"])
    upsert(
        session,
        AppState,
        dict(key=state_key, value={"requested_at": fetched_at.isoformat()}),
        ["key"],
    )
