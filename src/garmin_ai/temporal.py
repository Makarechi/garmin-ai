"""Versioned context: source time and system knowledge are separate clocks."""

from bisect import bisect_left
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.models import AppState, MetricObservation
from garmin_ai.projection_changes import execute_projection

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


def observation_key(fetched_at, metric, sequence, version):
    return f"{fetched_at.astimezone(UTC).isoformat()}|{metric}|{sequence}|{version}"


def preserve_observation_owners(session, ref):
    from garmin_ai.normalize import upsert

    key = f"observation-owner:{ref}"
    owner = session.get(AppState, key, populate_existing=True)
    times = dict(owner.value.get("times", {})) if owner else {}
    zones = dict(owner.value.get("zones", {})) if owner else {}
    for row in session.scalars(
        select(MetricObservation).where(MetricObservation.source_ref == ref)
    ):
        identity = observation_key(row.fetched_at, row.metric, row.sequence, row.feature_version)
        times.setdefault(identity, row.ingested_at.isoformat())
        zones.setdefault(identity, row.timezone)
    if times:
        upsert(
            session,
            AppState,
            {"key": key, "value": {"source_ref": str(ref), "times": times, "zones": zones}},
            ["key"],
        )


def observation_ingested_at(session, ref, fetched_at, metric, sequence):
    owner = session.get(AppState, f"observation-owner:{ref}", populate_existing=True)
    value = (
        owner.value.get("times", {}).get(
            observation_key(fetched_at, metric, sequence, FEATURE_VERSION)
        )
        if owner
        else None
    )
    return datetime.fromisoformat(value) if value else datetime.now(UTC)


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
    fetched_at = session.info.get("fetch_time") or datetime.now(UTC)
    binding = session.get(AppState, "account:garmin")
    account = binding.value.get("fingerprint") if binding else None
    applications = {fetched_at: timezone}
    owner = session.get(AppState, f"observation-owner:{ref}", populate_existing=True)
    if owner:
        for key in owner.value.get("times", {}):
            at, old_metric, old_sequence, version = key.rsplit("|", 3)
            if (old_metric, old_sequence, version) == (metric, str(sequence), FEATURE_VERSION):
                applications.setdefault(
                    datetime.fromisoformat(at), owner.value.get("zones", {}).get(key, timezone)
                )
    for fetched_at, application_zone in applications.items():
        previous = session.scalar(
            select(MetricObservation)
            .where(
                MetricObservation.metric == metric,
                MetricObservation.source_calendar_date == day,
                MetricObservation.sequence == sequence,
                MetricObservation.observed_at.is_not_distinct_from(observed_at),
                MetricObservation.feature_version == FEATURE_VERSION,
            )
            .order_by(
                MetricObservation.fetched_at.desc(),
                MetricObservation.ingested_at.desc(),
                MetricObservation.sequence.desc(),
            )
            .limit(1)
        )
        same = previous is not None and all(
            getattr(previous, key) == expected
            for key, expected in {
                "value": value,
                "unit": unit,
                "timezone": application_zone,
                "effective_start": effective_start,
                "account": account,
                "quality": "observed" if observed_at else "time_unknown",
            }.items()
        )
        execute = (
            session.execute if same else lambda statement: execute_projection(session, statement)
        )
        execute(
            insert(MetricObservation)
            .values(
                metric=metric,
                value=value,
                unit=unit,
                source_calendar_date=day,
                source_ref=ref,
                fetched_at=fetched_at,
                ingested_at=observation_ingested_at(session, ref, fetched_at, metric, sequence),
                timezone=application_zone,
                sequence=sequence,
                observed_at=observed_at,
                effective_start=effective_start,
                account=account,
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
