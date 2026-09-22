"""Preserve superseded measurements for historical knowledge cutoffs."""

from datetime import UTC, datetime

from sqlalchemy import func, select, tuple_

from garmin_ai.models import Measurement, MeasurementHistory, SourcePayload


def retain_measurement(
    session, row: Measurement, *, superseded_at: datetime, known_at: datetime | None = None
):
    if row.source_ref is None:
        return
    if known_at is None:
        with session.no_autoflush:
            source = session.get(SourcePayload, row.source_ref)
            if source is None:
                return
            prior_activation = session.scalar(
                select(func.max(MeasurementHistory.superseded_at)).where(
                    MeasurementHistory.ts == row.ts,
                    MeasurementHistory.metric == row.metric,
                    MeasurementHistory.source == row.source,
                )
            )
        known_at = prior_activation or source.fetched_at
    # A replay of an older payload must not erase a newer value's knowledge window.
    boundary = max(superseded_at, known_at)
    session.add(
        MeasurementHistory(
            ts=row.ts,
            metric=row.metric,
            source=row.source,
            value=row.value,
            metric_definition_version_id=row.metric_definition_version_id,
            source_ref=row.source_ref,
            quality=row.quality,
            known_at=known_at,
            superseded_at=boundary,
        )
    )


def retain_measurements_before_delete(session, *predicates, superseded_at=None):
    boundary = superseded_at or session.info.get("fetch_time") or datetime.now(UTC)
    rows = session.scalars(select(Measurement).where(*predicates)).yield_per(500)
    for batch in rows.partitions(500):
        references = {row.source_ref for row in batch if row.source_ref is not None}
        keys = {(row.ts, row.metric, row.source) for row in batch}
        sources = dict(
            session.execute(
                select(SourcePayload.id, SourcePayload.fetched_at).where(
                    SourcePayload.id.in_(references)
                )
            ).all()
        )
        activations = {
            (ts, metric, source): activated_at
            for ts, metric, source, activated_at in session.execute(
                select(
                    MeasurementHistory.ts,
                    MeasurementHistory.metric,
                    MeasurementHistory.source,
                    func.max(MeasurementHistory.superseded_at),
                )
                .where(
                    tuple_(
                        MeasurementHistory.ts,
                        MeasurementHistory.metric,
                        MeasurementHistory.source,
                    ).in_(keys)
                )
                .group_by(
                    MeasurementHistory.ts,
                    MeasurementHistory.metric,
                    MeasurementHistory.source,
                )
            )
        }
        for row in batch:
            fetched_at = sources.get(row.source_ref)
            if fetched_at is not None:
                retain_measurement(
                    session,
                    row,
                    superseded_at=boundary,
                    known_at=activations.get((row.ts, row.metric, row.source)) or fetched_at,
                )
        session.flush()
