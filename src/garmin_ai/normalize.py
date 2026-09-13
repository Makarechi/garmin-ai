"""Conservative Garmin parsers. Missing/invalid upstream values never erase history."""

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.metrics import CATALOG
from garmin_ai.models import (
    Activity,
    ActivityPart,
    AppState,
    HealthDay,
    Measurement,
    SourcePayload,
    TimelineInterval,
)
from garmin_ai.temporal import explicit_time, observe

PARSER_VERSION = 8


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
    scope = session.info.get("replay_owned_intervals")
    if model is TimelineInterval and scope is not None and values.get("id") not in scope:
        if session.get(TimelineInterval, values.get("id")) is not None:
            return
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
    lock_key = f"health-day:{day}"
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))
    existing = session.get(HealthDay, day, populate_existing=True)
    fetched_at = session.info.get("fetch_time") or datetime.now(UTC)
    if existing:
        fields = {
            key: value
            for key, value in fields.items()
            if not existing.sources.get(f"time:{key}")
            or fetched_at >= datetime.fromisoformat(existing.sources[f"time:{key}"])
        }
    if not fields:
        return
    stmt = insert(HealthDay).values(
        day=day,
        **fields,
        sources={
            **{f"field:{field}": str(ref) for field in fields},
            **{f"time:{field}": fetched_at.isoformat() for field in fields},
            f"payload:{ref}": endpoint,
        },
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
    if session.info.get("skip_samples"):
        return
    specification = CATALOG.get(metric)
    if specification:
        if unit != specification.unit or specification.kind == "daily_summary":
            return
        minimum = specification.minimum
        maximum = specification.maximum
    value = numeric(value, minimum=minimum, maximum=maximum)
    if value is None or ts is None:
        return
    ts = timestamp(ts)
    owner_scope = session.info.get("replay_owned_samples")
    if owner_scope is not None and (ts, metric, source) not in owner_scope:
        if session.get(Measurement, (ts, metric, source)) is not None:
            return
    replaced = session.info.setdefault("replaced_metrics", set())
    marker = (str(ref), metric)
    if marker not in replaced and owner_scope is None:
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
    session.info["normalizing_ref"] = str(ref)
    session.execute(select(func.pg_advisory_xact_lock(72104619)))
    session.info["replaced_metrics"] = set()
    try:
        return _normalize(session, endpoint, key, payload, ref, timezone)
    finally:
        session.info.pop("normalizing_ref", None)
        session.info.pop("replaced_metrics", None)
        session.info.pop("fetch_time", None)
        session.info.pop("skip_samples", None)


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
            observe(
                session,
                "sleep_score",
                fields["sleep_score"],
                "score",
                day,
                ref,
                timezone,
                observed_at=timestamp(end),
                effective_start=timestamp(start),
            )
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
            for sequence, row in enumerate(rows):
                observed = explicit_time(row.get("timestamp"))
                observe(
                    session,
                    "training_readiness_score",
                    numeric(row.get("score"), maximum=100),
                    "score",
                    day,
                    ref,
                    timezone,
                    sequence,
                    observed_at=observed,
                )
                observe(
                    session,
                    "recovery_time_minutes",
                    0
                    if row.get("recoveryTimeChangePhrase") == "REACHED_ZERO"
                    else numeric(row.get("recoveryTime")),
                    "minutes",
                    day,
                    ref,
                    timezone,
                    sequence,
                    observed_at=observed,
                )
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
        fields["hydration_ml"] = numeric(payload.get("valueInML"))
    elif endpoint == "max_metrics":
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            fields["vo2max"] = numeric((row.get("generic") or {}).get("vo2MaxPreciseValue"))
    else:
        return "archived"
    health_fields(session, day, fields, endpoint, ref)
    return "normalized"


def normalize_activity(session, payload, timezone):
    session.execute(select(func.pg_advisory_xact_lock(72104619)))
    summary = {**payload, **(payload.get("summaryDTO") or {})}
    identity = str(payload["activityId"])
    state_key = f"activity-version:{identity}"
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(state_key, 0))))
    fetched_at = session.info.get("fetch_time", datetime.now(UTC))
    state = session.get(AppState, state_key, populate_existing=True)
    rebuilding = session.info.get("rebuilding_activity")
    ref = session.info.get("normalizing_ref")
    owners = dict(state.value.get("owners", {})) if state else {}
    if rebuilding and state and not owners and not state.value.get("owners_initialized"):
        owners = legacy_activity_owners(session, identity)
    older = state and fetched_at < datetime.fromisoformat(state.value["requested_at"])
    if older and not (rebuilding and ref in owners.values()):
        return
    start = summary.get("startTimeGMT")
    duration = numeric(summary.get("duration"))
    if not start or duration is None:
        raise ValueError("Activity lacks GMT start or duration")
    start = timestamp(start)
    elapsed = numeric(summary.get("elapsedDuration")) or duration
    field_names = {
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
    }
    fields = {k: numeric(summary.get(v)) for k, v in field_names.items()}
    if fields.get("cadence") is None:
        fields["cadence"] = numeric(summary.get("averageRunCadence"))
    if fields.get("aerobic_effect") is None:
        fields["aerobic_effect"] = numeric(summary.get("trainingEffect"))
    fields = {
        k: v
        for k, v in fields.items()
        if (not older or owners.get(k, ref) == ref)
        and (not rebuilding or not owners or owners.get(k, ref) == ref)
        and (v is not None or (rebuilding and (owners.get(k) == ref or field_names[k] in summary)))
    }
    existing = session.get(Activity, identity)
    timezone = (payload.get("timeZoneUnitDTO") or {}).get("timeZone") or (
        existing.timezone if existing else timezone
    )
    # Validate source timezone before preserving it for activity-local analysis.
    ZoneInfo(timezone)
    kind = (payload.get("activityType") or payload.get("activityTypeDTO") or {}).get("typeKey") or (
        existing.kind if existing else "unknown"
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
    if older:
        values = {
            "id": identity,
            "start": existing.start,
            "end": existing.end,
            "timezone": existing.timezone,
            "kind": existing.kind,
            **fields,
        }
    owners.update({key: ref for key in (fields if older else values) if key != "id"})
    upsert(session, Activity, values, ["id"])
    upsert(
        session,
        AppState,
        dict(
            key=state_key,
            value={
                "requested_at": state.value["requested_at"] if older else fetched_at.isoformat(),
                "owners": owners,
                "owners_initialized": True,
            },
        ),
        ["key"],
    )


def legacy_activity_owners(session, identity):
    """Recover omitted-field provenance from successfully applied legacy summaries."""
    candidates = []
    for raw, metadata in session.execute(
        select(SourcePayload, AppState.value)
        .outerjoin(AppState, AppState.key == func.concat("ingest-meta:", SourcePayload.id))
        .where(
            SourcePayload.endpoint.in_(["activity", "activities"]),
            SourcePayload.status.in_(["normalized", "partial", "error"]),
            SourcePayload.parser_version > 0,
        )
    ):
        entries = raw.payload if isinstance(raw.payload, list) else [raw.payload]
        at = (
            datetime.fromisoformat(metadata["applied_at"])
            if metadata and metadata.get("applied_at")
            else raw.fetched_at
        )
        for entry in entries:
            if isinstance(entry, dict) and entry.get("activityId") is not None:
                candidates.append(
                    (
                        at,
                        str(raw.id),
                        str(entry["activityId"]),
                        {**entry, **(entry.get("summaryDTO") or {})},
                    )
                )
    owners = {}
    aliases = {
        "duration_seconds": ("duration",),
        "moving_seconds": ("movingDuration",),
        "distance_m": ("distance",),
        "avg_hr": ("averageHR",),
        "max_hr": ("maxHR",),
        "avg_speed_mps": ("averageSpeed",),
        "calories": ("calories",),
        "cadence": ("averageRunningCadenceInStepsPerMinute", "averageRunCadence"),
        "ascent_m": ("elevationGain",),
        "descent_m": ("elevationLoss",),
        "aerobic_effect": ("aerobicTrainingEffect", "trainingEffect"),
        "anaerobic_effect": ("anaerobicTrainingEffect",),
        "training_load": ("activityTrainingLoad",),
    }
    for _, ref, activity_id, summary in sorted(candidates, key=lambda item: item[:3]):
        for field, keys in aliases.items():
            if any(summary.get(key) is not None for key in keys):
                owners.setdefault(activity_id, {})[field] = ref
    # Materialize all legacy maps together so later activity/page jobs do not rescan the archive.
    for state in session.scalars(
        select(AppState).where(AppState.key.startswith("activity-version:"))
    ):
        if not state.value.get("owners") and not state.value.get("owners_initialized"):
            state.value = {
                **state.value,
                "owners": owners.get(state.key.split(":", 1)[1], {}),
                "owners_initialized": True,
            }
    session.flush()
    return owners.get(identity, {})
