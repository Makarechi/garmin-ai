"""Coverage-aware local-hour distribution of Garmin's measured stress score."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from garmin_ai.models import Measurement
from garmin_ai.queries import date_range

ELEVATED_SCORE = 51
MAX_GAP = timedelta(minutes=5)


def _split_local_hours(left, right, zone):
    while left < right:
        local = left.astimezone(zone)
        seconds = 3600 - (local.minute * 60 + local.second + local.microsecond / 1_000_000)
        stop = min(right, left + timedelta(seconds=seconds))
        yield local.hour, local.date(), (stop - left).total_seconds()
        left = stop


def stress_by_hour(session, start: date, end: date, timezone: str):
    """Rank local hours by elevated share of observed time, never by raw sample count."""
    date_range(start, end, 30)
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError("Unknown timezone") from None
    left = datetime.combine(start, time.min, zone).astimezone(UTC)
    right = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC)
    scheduled = [0.0] * 24
    for hour, _, seconds in _split_local_hours(left, right, zone):
        scheduled[hour] += seconds

    samples = session.execute(
        select(Measurement.ts, Measurement.value)
        .where(
            Measurement.metric == "stress_score",
            Measurement.source == "garmin_connect",
            Measurement.quality == "observed",
            Measurement.unit == "score",
            Measurement.value >= 0,
            Measurement.value <= 100,
            Measurement.ts >= left - MAX_GAP,
            Measurement.ts <= right + MAX_GAP,
        )
        .order_by(Measurement.ts)
        .limit(100001)
    ).all()
    if len(samples) > 100000:
        raise ValueError("Stress analysis exceeds 100000 samples; narrow the date range")

    covered = [0.0] * 24
    elevated = [0.0] * 24
    weighted = [0.0] * 24
    days = [set() for _ in range(24)]
    for first, second in zip(samples, samples[1:], strict=False):
        if not timedelta(0) < second.ts - first.ts <= MAX_GAP:
            continue
        segment_start, segment_end = max(first.ts, left), min(second.ts, right)
        if segment_start >= segment_end:
            continue
        for hour, local_day, seconds in _split_local_hours(segment_start, segment_end, zone):
            covered[hour] += seconds
            weighted[hour] += seconds * first.value
            if first.value >= ELEVATED_SCORE:
                elevated[hour] += seconds
            days[hour].add(local_day)

    rows = []
    for hour in range(24):
        ratio = covered[hour] / scheduled[hour] if scheduled[hour] else 0
        eligible = ratio >= 0.5 and len(days[hour]) >= 3
        rows.append(
            {
                "hour": hour,
                "observed_days": len(days[hour]),
                "coverage_ratio": round(ratio, 4),
                "observed_hours": round(covered[hour] / 3600, 2),
                "elevated_share": round(elevated[hour] / covered[hour], 4) if eligible else None,
                "mean_score": round(weighted[hour] / covered[hour], 1) if eligible else None,
            }
        )
    ranked = sorted(
        (row for row in rows if row["elevated_share"] is not None),
        key=lambda row: (-row["elevated_share"], -row["coverage_ratio"], row["hour"]),
    )
    return {
        "metric": "stress_score",
        "source": "garmin_connect",
        "timezone": timezone,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "status": "ok" if ranked else "insufficient_coverage",
        "threshold": ELEVATED_SCORE,
        "ranked_hours": ranked,
        "hours": rows,
        "method": "Share of observed time with Garmin stress score at or above threshold; gaps over five minutes are excluded",
        "limitations": [
            "Garmin stress score is a physiological estimate, not a diary report of feeling stressed",
            "Hours with under 50% coverage or fewer than three observed days are not ranked",
            "Missing measurements are not treated as low stress",
        ],
    }
