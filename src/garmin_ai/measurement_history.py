"""Preserve superseded measurements for historical knowledge cutoffs."""

from datetime import UTC, datetime

from sqlalchemy import select

from garmin_ai.models import Measurement, MeasurementHistory, SourcePayload


def retain_measurement(session, row: Measurement, *, superseded_at: datetime):
    if row.source_ref is None:
        return
    source = session.get(SourcePayload, row.source_ref)
    if source is None:
        return
    # A replay of an older payload must not erase a newer value's knowledge window.
    boundary = max(superseded_at, source.fetched_at)
    session.add(
        MeasurementHistory(
            ts=row.ts,
            metric=row.metric,
            source=row.source,
            value=row.value,
            metric_definition_version_id=row.metric_definition_version_id,
            source_ref=row.source_ref,
            quality=row.quality,
            known_at=source.fetched_at,
            superseded_at=boundary,
        )
    )
    session.flush()


def retain_measurements_before_delete(session, *predicates, superseded_at=None):
    boundary = superseded_at or session.info.get("fetch_time") or datetime.now(UTC)
    for row in session.scalars(select(Measurement).where(*predicates)).all():
        retain_measurement(session, row, superseded_at=boundary)
