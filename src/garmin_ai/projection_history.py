"""Reconstruct overwritten partial observations without undoing attested deletions."""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, or_, select

from garmin_ai.measurement_history import retain_measurements_before_delete
from garmin_ai.metrics import CATALOG
from garmin_ai.models import AppState, Measurement, SourcePayload
from garmin_ai.normalize import normalize, numeric, timestamp, upsert
from garmin_ai.reconciliation import ENDPOINT_METRICS, Replacement

LIMIT = 1000


def history_key(raw):
    return f"ingest-history:{raw.source}:{raw.endpoint}:{raw.source_key}"


def load_history(session, raw):
    if raw.endpoint not in ENDPOINT_METRICS:
        return []
    stored = session.get(AppState, history_key(raw), populate_existing=True)
    if stored:
        return list(stored.value["applications"])
    # A raw's creation time cannot recover pre-journal A -> B -> A order.
    # Preserve an unknown-history boundary while allowing new observations.
    owns_measurement = (
        select(Measurement.source_ref)
        .where(Measurement.source_ref == SourcePayload.id)
        .correlate(SourcePayload)
        .exists()
    )
    legacy = session.execute(
        select(SourcePayload, owns_measurement).where(
            SourcePayload.source == raw.source,
            SourcePayload.endpoint == raw.endpoint,
            SourcePayload.source_key == raw.source_key,
            SourcePayload.id != raw.id,
            or_(
                SourcePayload.status.in_(["normalized", "partial"]),
                owns_measurement,
            ),
        )
    )
    return (
        [{"legacy_order_unknown": True}]
        if any(owned or could_emit_samples(candidate) for candidate, owned in legacy)
        else []
    )


def could_emit_samples(raw):
    def accepted(ts, value, metric):
        spec = CATALOG[metric]
        if ts is None or numeric(value, minimum=spec.minimum, maximum=spec.maximum) is None:
            return False
        try:
            timestamp(ts)
            return True
        except (ValueError, TypeError, OverflowError, OSError):
            return False

    payload = raw.payload
    if raw.endpoint == "steps":
        return any(
            accepted(point.get("startGMT"), point.get("steps"), "steps_bucket") for point in payload
        )
    if raw.endpoint == "hrv":
        return any(
            accepted(point.get("readingTimeGMT"), point.get("hrvValue"), "hrv_rmssd_ms")
            for point in payload.get("hrvReadings") or []
        )
    arrays = {
        "heart_rate": ("heartRateValues", "heart_rate_bpm"),
        "stress": ("stressValuesArray", "stress_score"),
        "respiration": ("respirationValuesArray", "respiration_rpm"),
        "spo2": (
            "spO2HourlyAverages" if "spO2HourlyAverages" in payload else "spO2ValuesArray",
            "spo2_pct",
        ),
    }
    if raw.endpoint not in arrays:
        return True
    array, metric = arrays[raw.endpoint]
    if any(
        isinstance(point, list) and len(point) >= 2 and accepted(point[0], point[1], metric)
        for point in payload.get(array) or []
    ):
        return True
    if raw.endpoint == "stress":
        index = next(
            (
                int(item["bodyBatteryValueDescriptorIndex"])
                for item in payload.get("bodyBatteryValueDescriptorsDTOList") or []
                if item.get("bodyBatteryValueDescriptorKey") == "bodyBatteryLevel"
            ),
            None,
        )
        if index is not None:
            return any(
                len(point) > index and accepted(point[0], point[index], "body_battery")
                for point in payload.get("bodyBatteryValuesArray") or []
            )
    return False


def record_application(session, raw, history, timezone, at, replacement, *, replay=False):
    if raw.endpoint not in ENDPOINT_METRICS:
        return
    # Only structurally empty responses lack future sample evidence. Preserve
    # nonempty parser-rejected representations and their application order.
    empty = raw.payload in (None, {}, []) or (
        isinstance(raw.payload, dict)
        and all(value in (None, {}, []) for value in raw.payload.values())
    )
    if not replacement and empty:
        return
    entry = {
        "raw_ref": str(raw.id),
        "at": at.isoformat(),
        "timezone": timezone,
        "replacement": replacement,
    }
    # A parser-only replay does not add another source application.
    if replay and entry in history:
        return
    if len(history) >= LIMIT:
        raise ValueError("Partial revision history exceeds reconstruction budget")
    upsert(
        session,
        AppState,
        {"key": history_key(raw), "value": {"applications": [*history, entry]}},
        ["key"],
    )


def previous_observations(session, archive, raw, history):
    if raw.endpoint not in ENDPOINT_METRICS:
        return []
    targets = set(
        session.execute(
            select(Measurement.ts, Measurement.metric, Measurement.source).where(
                Measurement.source_ref == raw.id
            )
        ).all()
    )
    if not targets:
        return []
    restored = {}
    unknown_targets = set()
    for application in history:
        if application.get("legacy_order_unknown"):
            unknown_targets.update(targets)
            restored.clear()
            continue
        previous = session.get(SourcePayload, UUID(application["raw_ref"]))
        if previous is None or application.get("timezone") is None:
            raise ValueError("Historical projection provenance is unavailable")
        replacement = Replacement.restore(application.get("replacement"))
        if replacement:
            replacement.validate(raw.endpoint)
            unknown_targets = {
                key
                for key in unknown_targets
                if not (
                    key[1] in replacement.metrics and replacement.start <= key[0] < replacement.end
                )
            }
            restored = {
                key: value
                for key, value in restored.items()
                if not (
                    key[1] in replacement.metrics and replacement.start <= key[0] < replacement.end
                )
            }
        data = archive.read(previous.archive_key)
        if hashlib.sha256(data).hexdigest() != previous.payload_hash:
            raise ValueError("Historical source hash mismatch")
        info = dict(session.info)
        savepoint = session.begin_nested()
        try:
            retain_measurements_before_delete(session, Measurement.source_ref == previous.id)
            session.execute(delete(Measurement).where(Measurement.source_ref == previous.id))
            session.info["fetch_time"] = datetime.fromisoformat(application["at"])
            session.info["skip_samples"] = False
            normalize(
                session,
                previous.endpoint,
                previous.source_key,
                json.loads(data),
                previous.id,
                application["timezone"],
            )
            for measurement in session.scalars(
                select(Measurement)
                .where(Measurement.source_ref == previous.id)
                .execution_options(populate_existing=True)
            ):
                key = (measurement.ts, measurement.metric, measurement.source)
                if key in targets:
                    unknown_targets.discard(key)
                    restored[key] = {
                        column.name: getattr(measurement, column.name)
                        for column in Measurement.__table__.columns
                    }
        finally:
            savepoint.rollback()
            session.info.clear()
            session.info.update(info)
    if unknown_targets:
        raise ValueError(
            "Legacy partial application order is unavailable; rebuild requires verified history"
        )
    # The fallback belongs to the effective current projection. Its original
    # provenance remains in the application journal for the next parser rebuild.
    return [{**value, "source_ref": raw.id} for value in restored.values()]
