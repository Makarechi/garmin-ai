"""Versioned context: source time and system knowledge are separate clocks."""

from bisect import bisect_left
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.models import AppState, MetricObservation

FEATURE_VERSION = "pre-event-v1"


def explicit_time(value):
    """Do not assign UTC to an undocumented naive source timestamp."""
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except ValueError:
        return None


def observe(
    session,
    metric,
    value,
    unit,
    day,
    ref,
    timezone,
    sequence=0,
    observed_at=None,
    effective_start=None,
):
    if value is None:
        return
    binding = session.get(AppState, "account:garmin")
    session.execute(
        insert(MetricObservation)
        .values(
            metric=metric,
            value=value,
            unit=unit,
            source_calendar_date=day,
            source_ref=ref,
            fetched_at=session.info.get("fetch_time") or datetime.now(UTC),
            ingested_at=datetime.now(UTC),
            timezone=timezone,
            sequence=sequence,
            observed_at=observed_at,
            effective_start=effective_start,
            account=binding.value.get("fingerprint") if binding else None,
            device=None,
            quality="observed" if observed_at else "time_unknown",
            feature_version=FEATURE_VERSION,
        )
        .on_conflict_do_nothing()
    )


def feature_at(session, metric, event_time, knowledge_cutoff, purpose="retrospective"):
    return features_at(session, [metric], [event_time], knowledge_cutoff, purpose)[event_time][
        metric
    ]


def features_at(session, metrics, event_times, knowledge_cutoff, purpose="retrospective"):
    if purpose not in {"retrospective", "as_known"}:
        raise ValueError("Unknown temporal purpose")
    if knowledge_cutoff.tzinfo is None or any(at.tzinfo is None for at in event_times):
        raise ValueError("Temporal cutoffs must be timezone-aware")
    if purpose == "as_known" and any(knowledge_cutoff > at for at in event_times):
        raise ValueError("As-known cutoff cannot follow the event")
    # A bounded age prevents accidentally attaching an old night during travel/gaps.
    if not event_times:
        return {}
    observations = session.scalars(
        select(MetricObservation)
        .where(
            MetricObservation.metric.in_(metrics),
            MetricObservation.observed_at < max(event_times),
            MetricObservation.observed_at >= min(event_times) - timedelta(hours=24),
            MetricObservation.ingested_at <= knowledge_cutoff,
            MetricObservation.quality == "observed",
            MetricObservation.feature_version == FEATURE_VERSION,
        )
        .order_by(
            MetricObservation.observed_at.desc(),
            MetricObservation.fetched_at.desc(),
            MetricObservation.ingested_at.desc(),
            MetricObservation.sequence.desc(),
        )
        .limit(100001)
    ).all()
    if len(observations) > 100000:
        raise ValueError("Temporal context exceeds 100000 versions; narrow the activity range")
    indexed = {metric: {} for metric in metrics}
    for row in observations:
        indexed[row.metric].setdefault(row.observed_at, row)
    times = {metric: sorted(rows) for metric, rows in indexed.items()}
    result = {}
    for at in event_times:
        result[at] = {}
        for metric in metrics:
            index = bisect_left(times[metric], at) - 1
            row = indexed[metric][times[metric][index]] if index >= 0 else None
            if row and row.observed_at < at - timedelta(hours=24):
                row = None
            result[at][metric] = feature_result(row, at, knowledge_cutoff, purpose)
    return result


def feature_result(row, event_time, knowledge_cutoff, purpose):
    return {
        "value": row.value if row else None,
        "event_cutoff": event_time.isoformat(),
        "knowledge_cutoff": knowledge_cutoff.isoformat(),
        "purpose": purpose,
        "feature_version": FEATURE_VERSION,
        "observation_id": str(row.id) if row else None,
        "source_ref": str(row.source_ref) if row else None,
        "observed_at": row.observed_at.isoformat() if row else None,
        "ingested_at": row.ingested_at.isoformat() if row else None,
        "effective_start": row.effective_start.isoformat() if row and row.effective_start else None,
        "interpretation_timezone": row.timezone if row else None,
        "source_calendar_date": str(row.source_calendar_date) if row else None,
        "quality": "observed" if row else "unknown",
    }
