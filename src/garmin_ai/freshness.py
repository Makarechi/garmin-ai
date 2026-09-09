"""Observation recency and conservative coverage, separate from fetch success."""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from garmin_ai.models import HealthDay, Measurement, SourcePayload

# Engineering quality policies, not medical thresholds or vendor wear guarantees.
CHANNELS = {
    "heart_rate_bpm": ("heart_rate", 300, 1800),
    "stress_score": ("stress", 300, 1800),
    "body_battery": ("stress", 900, 3600),
    "respiration_rpm": ("respiration", 300, 1800),
    "spo2_pct": ("spo2", 3600, 7200),
}
DAILY_CHANNELS = {
    "hrv_nightly_avg": "hrv",
    "sleep_score": "sleep",
    "training_readiness_score": "readiness",
}


def covered_seconds(timestamps, left, right, max_gap_seconds):
    """Union of intervals between adjacent samples; no fill across gaps or after last sample."""
    points = sorted(set(timestamps))
    return sum(
        max(0, (min(b, right) - max(a, left)).total_seconds())
        for a, b in zip(points, points[1:], strict=False)
        if (b - a).total_seconds() <= max_gap_seconds
    )


def observation_freshness(session, now, timezone, endpoints):
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    left = datetime.combine(today, datetime.min.time(), zone).astimezone(UTC)
    elapsed = (now - left).total_seconds()
    result = {}
    for metric, (endpoint, max_gap, max_lag) in CHANNELS.items():
        points = session.scalars(
            select(Measurement.ts)
            .where(
                Measurement.metric == metric,
                Measurement.quality == "observed",
                Measurement.ts >= now - timedelta(days=2),
                Measurement.ts <= now,
            )
            .order_by(Measurement.ts)
        ).all()
        latest = session.execute(
            select(Measurement.ts, Measurement.source_ref)
            .where(
                Measurement.metric == metric,
                Measurement.quality == "observed",
                Measurement.ts <= now,
            )
            .order_by(Measurement.ts.desc(), Measurement.source)
            .limit(1)
        ).first()
        newest = latest.ts if latest else None
        lag = (now - newest).total_seconds() if newest else None
        covered = covered_seconds(points, left, now, max_gap)
        recent_left = now - timedelta(seconds=max_lag)
        recent_ratio = covered_seconds(points, recent_left, now, max_gap) / max_lag
        technical = endpoints.get(endpoint, {})
        fetch_status = technical.get("status")
        if technical.get("source_ref") and fetch_status not in {"error", "fetch_error"}:
            has_samples = session.scalar(
                select(Measurement.ts)
                .where(
                    Measurement.source_ref == UUID(technical["source_ref"]),
                    Measurement.metric == metric,
                    Measurement.quality == "observed",
                    Measurement.ts <= now,
                )
                .limit(1)
            )
            if has_samples is None:
                fetch_status = "empty"
        if fetch_status in {"empty", "error", "fetch_error"}:
            quality = {
                "empty": "source_empty",
                "error": "parser_error",
                "fetch_error": "fetch_error",
            }[fetch_status]
        elif newest is None:
            quality = "not_synced" if not technical else "unknown"
        elif lag > max_lag:
            quality = "stale_observation"
        elif recent_ratio < 0.8:
            quality = "partial"
        else:
            quality = "recent_observations"
        result[metric] = {
            "endpoint": endpoint,
            "semantics": "intraday_samples",
            "newest_observed_at": newest.isoformat() if newest else None,
            "observation_lag_seconds": lag,
            "expected_interval": {"start": left.isoformat(), "end": now.isoformat()},
            "observed_interval": {
                "start": points[0].isoformat() if points else None,
                "end": newest.isoformat() if newest else None,
            },
            "coverage_ratio": covered / elapsed if elapsed > 0 else None,
            "covered_seconds": covered,
            "recent_coverage_ratio": recent_ratio,
            "max_gap_seconds": max_gap,
            "max_lag_seconds": max_lag,
            "quality_reason": quality,
            "usable_for_current_state": quality == "recent_observations",
            "source_updated_at": technical.get("source_updated_at"),
            "source_ref": str(latest.source_ref) if latest and latest.source_ref else None,
            "fetch_source_ref": technical.get("source_ref"),
        }
    for metric, endpoint in DAILY_CHANNELS.items():
        daily = session.scalar(
            select(HealthDay)
            .where(HealthDay.day <= today, getattr(HealthDay, metric).is_not(None))
            .order_by(HealthDay.day.desc())
            .limit(1)
        )
        age_days = (today - daily.day).days if daily else None
        recent = age_days is not None and age_days <= (1 if endpoint in {"hrv", "sleep"} else 0)
        result[metric] = {
            "endpoint": endpoint,
            "semantics": "daily_summary",
            "source_calendar_date": str(daily.day) if daily else None,
            "age_calendar_days": age_days,
            "newest_observed_at": None,
            "observation_lag_seconds": None,
            "coverage_ratio": None,
            "quality_reason": "recent_daily_summary"
            if recent
            else "unknown"
            if daily
            else "not_synced",
            "usable_for_current_state": False,
            "usable_as_daily_summary": recent,
            "source_ref": daily.sources.get(f"field:{metric}") if daily else None,
        }
    return result


def source_metadata(session, source_ref):
    from uuid import UUID

    if not source_ref:
        return {}
    row = session.get(SourcePayload, UUID(source_ref))
    if row is None:
        return {}
    return {
        "source_ref": str(row.id),
        "source_updated_at": row.source_updated_at.isoformat() if row.source_updated_at else None,
        "source_revision": row.payload_hash,
        "parser_version": row.parser_version,
        "status": row.status,
    }
