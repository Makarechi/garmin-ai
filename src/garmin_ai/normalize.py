"""Conservative Garmin parsers. Missing/invalid upstream values never erase history."""

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
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
from garmin_ai.projection_changes import execute_projection
from garmin_ai.temporal import explicit_time, observe

PARSER_VERSION = 9


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


def replay_is_newer_than(session, owner):
    if not owner or not session.info.get("fetch_time"):
        return False
    metadata = session.get(AppState, f"ingest-meta:{owner}", populate_existing=True)
    raw = session.get(SourcePayload, owner, populate_existing=True)
    if raw is None:
        return False
    applied_at = (
        datetime.fromisoformat(metadata.value["applied_at"])
        if metadata and metadata.value.get("applied_at")
        else raw.fetched_at
    )
    return session.info["fetch_time"] > applied_at


def upsert(session, model, values, keys):
    scope = session.info.get("replay_owned_intervals")
    if model is TimelineInterval and scope is not None and values.get("id") not in scope:
        existing = session.get(TimelineInterval, values.get("id"), populate_existing=True)
        tombstone = session.get(
            AppState, f"interval-owner:{values.get('id')}", populate_existing=True
        )
        owner = (
            existing.evidence.get("source_ref")
            if existing
            else (tombstone.value.get("source_ref") if tombstone else None)
        )
        if (
            owner
            and owner != session.info.get("normalizing_ref")
            and not replay_is_newer_than(session, owner)
        ):
            return
    if model is TimelineInterval:
        session.execute(
            delete(AppState).where(AppState.key == f"interval-owner:{values.get('id')}")
        )
    stmt = insert(model).values(**values)
    updates = {key: getattr(stmt.excluded, key) for key in values if key not in keys}
    if "updated_at" in model.__table__.columns:
        updates["updated_at"] = func.now()
    if updates:
        stmt = stmt.on_conflict_do_update(
            index_elements=keys,
            set_=updates,
            where=or_(
                *(
                    getattr(model, key).is_distinct_from(getattr(stmt.excluded, key))
                    for key in values
                    if key not in keys and key != "updated_at"
                )
            ),
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=keys)
    provenance_only = False
    if model is TimelineInterval:
        existing = session.get(model, values["id"], populate_existing=True)
        provenance_only = existing is not None and all(
            (
                {k: v for k, v in existing.evidence.items() if k != "source_ref"}
                == {k: v for k, v in value.items() if k != "source_ref"}
            )
            if key == "evidence"
            else getattr(existing, key) == value
            for key, value in values.items()
            if key != "updated_at"
        )
    if model is Measurement:
        existing = session.get(model, tuple(values[key] for key in keys), populate_existing=True)
        if existing is not None and any(
            getattr(existing, key) != value for key, value in values.items() if key != "updated_at"
        ):
            from garmin_ai.measurement_history import retain_measurement

            retain_measurement(
                session,
                existing,
                superseded_at=session.info.get("fetch_time") or datetime.now(UTC),
            )
        provenance_only = existing is not None and all(
            getattr(existing, key) == value
            for key, value in values.items()
            if key not in {"source_ref", "updated_at"}
        )
        replacement_scope = session.info.get("replacement_scope")
        if replacement_scope:
            source, contract = replacement_scope
            if (
                values["source"] == source
                and values["metric"] in contract.metrics
                and contract.start <= values["ts"] < contract.end
            ):
                provenance_only = True  # Compare the final interval after reinserting all points.
    if (
        model in {Measurement, HealthDay, TimelineInterval, Activity, ActivityPart}
        and not provenance_only
    ):
        execute_projection(session, stmt)
    else:
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
            or fetched_at > datetime.fromisoformat(existing.sources[f"time:{key}"])
            or (
                fetched_at == datetime.fromisoformat(existing.sources[f"time:{key}"])
                and (
                    (
                        not session.info.get("replaying_projection")
                        and session.info.get("replay_owned_samples") is None
                    )
                    or existing.sources.get(f"field:{key}")
                    in {None, str(ref), session.info.get("replay_preceding_source")}
                )
            )
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
    changed = existing is None or any(
        getattr(existing, key) != value for key, value in fields.items()
    )
    statement = stmt.on_conflict_do_update(index_elements=[HealthDay.day], set_=values)
    if changed:
        execute_projection(session, statement)
    else:
        # Advance source ownership/timing even for equal values, without invalidating insights.
        session.execute(statement)


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
    source=None,
):
    from garmin_ai.metric_definitions import ensure_system_metric_definitions

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
    sample_source = source or session.info.get("sample_source", "garmin_connect")
    if owner_scope is not None and (ts, metric, sample_source) not in owner_scope:
        existing = session.get(Measurement, (ts, metric, sample_source), populate_existing=True)
        tombstone = session.get(
            AppState,
            f"sample-owner:{ts.isoformat()}:{metric}:{sample_source}",
            populate_existing=True,
        )
        owner = (
            existing.source_ref
            if existing
            else (tombstone.value.get("source_ref") if tombstone else None)
        )
        if owner and str(owner) != str(ref):
            order = session.info.get("replay_source_order", {})
            if order.get(str(owner), len(order)) >= order.get(str(ref), -1):
                return
        if any(
            metric in contract.metrics and contract.start <= ts < contract.end
            for contract in session.info.get("replay_replacements", [])
        ):
            return
    # A shorter nonempty response does not attest a complete source snapshot.
    session.execute(
        delete(AppState).where(
            AppState.key == f"sample-owner:{ts.isoformat()}:{metric}:{sample_source}"
        )
    )
    metric_versions = session.info.get("system_metric_versions")
    if metric_versions is None:
        metric_versions = ensure_system_metric_definitions(session)
        session.info["system_metric_versions"] = metric_versions
    upsert(
        session,
        Measurement,
        dict(
            ts=ts,
            metric=metric,
            source=source or session.info.get("sample_source", "garmin_connect"),
            local_date=ts.astimezone(ZoneInfo(timezone)).date(),
            value=value,
            unit=unit,
            metric_definition_version_id=metric_versions[metric].id,
            source_ref=ref,
        ),
        ["ts", "metric", "source"],
    )


def normalize(session, endpoint: str, key: str, payload, ref, timezone: str):
    session.info["normalizing_ref"] = str(ref)
    session.execute(select(func.pg_advisory_xact_lock(72104619)))
    raw = session.get(SourcePayload, ref)
    session.info["sample_source"] = raw.source if raw else "garmin_connect"
    session.info["replaced_metrics"] = set()
    try:
        return _normalize(session, endpoint, key, payload, ref, timezone)
    finally:
        session.info.pop("normalizing_ref", None)
        session.info.pop("replaced_metrics", None)
        session.info.pop("fetch_time", None)
        session.info.pop("skip_samples", None)
        session.info.pop("sample_source", None)


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
            upsert(
                session,
                AppState,
                {
                    "key": f"activity-parts-owner:{key}:{endpoint}",
                    "value": {"source_ref": str(ref)},
                },
                ["key"],
            )
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
                effective_end=timestamp(end),
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
    if state and not owners and not state.value.get("owners_initialized"):
        owners = legacy_activity_owners(session, identity)
    older = state and fetched_at < datetime.fromisoformat(state.value["requested_at"])
    owner_times = {}
    if rebuilding and owners:
        for raw, metadata in session.execute(
            select(SourcePayload, AppState.value)
            .outerjoin(AppState, AppState.key == func.concat("ingest-meta:", SourcePayload.id))
            .where(SourcePayload.id.in_(set(owners.values())))
        ):
            owner_times[str(raw.id)] = (
                datetime.fromisoformat(metadata["applied_at"])
                if metadata and metadata.get("applied_at")
                else raw.fetched_at
            )

    legacy_owner_fields = set(state.value.get("legacy_owner_fields", [])) if state else set()

    def may_replace(key):
        owner = owners.get(key)
        if key in legacy_owner_fields and owner is not None and owner != ref:
            return False
        return (
            owner is None
            or owner == ref
            or (owner in owner_times and fetched_at > owner_times[owner])
        )

    if older and not (rebuilding and ref in owners.values()):
        return
    start = summary.get("startTimeGMT")
    duration = numeric(summary.get("duration"))
    installed = session.get(Activity, identity, populate_existing=True)
    preserve_timing = rebuilding and installed and not (may_replace("start") and may_replace("end"))
    if preserve_timing:
        start = installed.start
        elapsed = (installed.end - installed.start).total_seconds()
    else:
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
        if (not rebuilding or may_replace(k))
        and (v is not None or (rebuilding and owners.get(k) == ref))
    }
    existing = session.get(Activity, identity, populate_existing=True)
    supplied_timezone = (payload.get("timeZoneUnitDTO") or {}).get("timeZone")
    timezone = supplied_timezone or (existing.timezone if existing else timezone)
    # Validate source timezone before preserving it for activity-local analysis.
    ZoneInfo(timezone)
    supplied_kind = (payload.get("activityType") or payload.get("activityTypeDTO") or {}).get(
        "typeKey"
    )
    kind = supplied_kind or (existing.kind if existing else "unknown")
    values = dict(
        id=identity,
        kind=kind,
        start=start,
        end=start + timedelta(seconds=elapsed),
        timezone=timezone,
        **fields,
    )
    name_values = {}
    if (not rebuilding or may_replace("name")) and (
        payload.get("activityName") is not None or (rebuilding and owners.get("name") == ref)
    ):
        name_values["name"] = payload.get("activityName")
    values.update(name_values)
    metadata_values = {
        key: value
        for key, value in {"kind": supplied_kind, "timezone": supplied_timezone}.items()
        if value and (not rebuilding or may_replace(key))
    }
    if older:
        values = {
            "id": identity,
            "start": existing.start,
            "end": existing.end,
            "timezone": existing.timezone,
            "kind": existing.kind,
            **fields,
            **name_values,
            **metadata_values,
        }
    else:
        for key in ("kind", "timezone"):
            if rebuilding and not may_replace(key) and existing:
                values[key] = getattr(existing, key)
    if preserve_timing:
        values.update(start=installed.start, end=installed.end)
    owned_values = {**fields, **name_values, **metadata_values}
    if not older and not preserve_timing:
        owned_values.update(start=start, end=values["end"])
    owners.update({key: ref for key in owned_values})
    if not rebuilding:
        legacy_owner_fields.difference_update(owned_values)
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
                "legacy_owner_fields": sorted(legacy_owner_fields),
            },
        ),
        ["key"],
    )


def legacy_activity_number(value):
    # Pre-ownership parsers accepted finite, nonnegative numeric activity fields.
    # Keep this historical contract independent of future numeric-parser changes.
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
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
                        entry,
                        raw.status != "error" or bool(metadata and metadata.get("applied_at")),
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
    installed = {row.id: row for row in session.scalars(select(Activity))}
    chosen = {}
    latest_field_at = {}
    for at, ref, activity_id, entry, applied in sorted(candidates, key=lambda item: item[0]):
        summary = {**entry, **(entry.get("summaryDTO") or {})}
        values = {
            "name": entry.get("activityName"),
            "kind": (entry.get("activityType") or entry.get("activityTypeDTO") or {}).get(
                "typeKey"
            ),
            "timezone": (entry.get("timeZoneUnitDTO") or {}).get("timeZone"),
        }
        values.update(
            {
                field: next(
                    (summary[key] for key in keys if legacy_activity_number(summary.get(key))), None
                )
                for field, keys in aliases.items()
            }
        )
        # Timing was always written together by the legacy activity parser.
        # Recover it before a tied retained revision can claim unowned fields.
        duration = summary.get("duration")
        if summary.get("startTimeGMT") and legacy_activity_number(duration):
            try:
                start = timestamp(summary["startTimeGMT"])
                elapsed = summary.get("elapsedDuration")
                elapsed = elapsed if legacy_activity_number(elapsed) and elapsed else duration
                values.update(start=start, end=start + timedelta(seconds=elapsed))
            except (TypeError, ValueError, OverflowError):
                pass
        for field, value in values.items():
            if value is None:
                continue
            previous = chosen.get((activity_id, field))
            current = installed.get(activity_id)
            matches = current is not None and getattr(current, field) == value
            if not applied and (
                not matches or (previous is not None and previous[1] and previous[2])
            ):
                continue
            if applied:
                latest_field_at[(activity_id, field)] = max(
                    at, latest_field_at.get((activity_id, field), at)
                )
            if (
                previous is None
                or (matches and not previous[1])
                or (matches == previous[1] and at > previous[0])
            ):
                chosen[(activity_id, field)] = (at, matches, applied)
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
                # Matching installed values outrank original raw creation times.
                # When these disagree, the lost reapplication clock cannot
                # authorize another retained revision to overwrite that field.
                "legacy_owner_fields": [
                    field
                    for (activity_id, field), (at, matches, _) in chosen.items()
                    if activity_id == state.key.split(":", 1)[1]
                    and matches
                    and at < latest_field_at.get((activity_id, field), at)
                ],
            }
    session.flush()
    return owners.get(identity, {})
