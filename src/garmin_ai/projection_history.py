"""Reconstruct overwritten partial observations without undoing attested deletions."""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select

from garmin_ai.models import AppState, Measurement, SourcePayload
from garmin_ai.normalize import normalize, upsert
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
    legacy = session.scalar(
        select(SourcePayload.id)
        .where(
            SourcePayload.source == raw.source,
            SourcePayload.endpoint == raw.endpoint,
            SourcePayload.source_key == raw.source_key,
            SourcePayload.id != raw.id,
            SourcePayload.status.in_(["normalized", "partial", "empty"]),
        )
        .limit(1)
    )
    return [{"legacy_order_unknown": True}] if legacy is not None else []


def record_application(session, raw, history, timezone, at, replacement):
    if raw.endpoint not in ENDPOINT_METRICS:
        return
    entry = {
        "raw_ref": str(raw.id),
        "at": at.isoformat(),
        "timezone": timezone,
        "replacement": replacement,
    }
    # A parser-only replay does not add another source application.
    if history and history[-1] == entry:
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
    if any(application.get("legacy_order_unknown") for application in history):
        raise ValueError(
            "Legacy partial application order is unavailable; rebuild requires verified history"
        )
    restored = {}
    for application in history:
        previous = session.get(SourcePayload, UUID(application["raw_ref"]))
        if previous is None or application.get("timezone") is None:
            raise ValueError("Historical projection provenance is unavailable")
        replacement = Replacement.restore(application.get("replacement"))
        if replacement:
            replacement.validate(raw.endpoint)
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
                select(Measurement).where(Measurement.source_ref == previous.id)
            ):
                key = (measurement.ts, measurement.metric, measurement.source)
                if key in targets:
                    restored[key] = {
                        column.name: getattr(measurement, column.name)
                        for column in Measurement.__table__.columns
                    }
        finally:
            savepoint.rollback()
            session.info.clear()
            session.info.update(info)
    # The fallback belongs to the effective current projection. Its original
    # provenance remains in the application journal for the next parser rebuild.
    return [{**value, "source_ref": raw.id} for value in restored.values()]
