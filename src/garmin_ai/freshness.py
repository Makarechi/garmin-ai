"""Observation recency and conservative coverage, separate from fetch success."""

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from garmin_ai.models import HealthDay, Measurement, SourcePayload

# Engineering quality policies, not medical thresholds or vendor wear guarantees.
CHANNELS = {
    "heart_rate_bpm": ("heart_rate", 300, 1800, "frequent"),
    "stress_score": ("stress", 300, 1800, "frequent"),
    "body_battery": ("stress", 900, 3600, "frequent"),
    "respiration_rpm": ("respiration", 300, 1800, "daily"),
    "spo2_pct": ("spo2", 3600, 7200, "daily"),
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
    for metric, (endpoint, max_gap, max_lag, refresh_mode) in CHANNELS.items():
        points = session.scalars(
            select(Measurement.ts)
            .where(
                Measurement.metric == metric,
                Measurement.source == "garmin_connect",
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
                Measurement.source == "garmin_connect",
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
                    Measurement.source == "garmin_connect",
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
        today_points = [point for point in points if left <= point <= now]
        result[metric] = {
            "endpoint": endpoint,
            "semantics": "intraday_samples",
            "refresh_mode": refresh_mode,
            "newest_observed_at": newest.isoformat() if newest else None,
            "observation_lag_seconds": lag,
            "expected_interval": {"start": left.isoformat(), "end": now.isoformat()},
            "observed_interval": {
                "start": today_points[0].isoformat() if today_points else None,
                "end": today_points[-1].isoformat() if today_points else None,
            },
            "coverage_ratio": covered / elapsed if elapsed > 0 else None,
            "covered_seconds": covered,
            "recent_coverage_ratio": recent_ratio,
            "max_gap_seconds": max_gap,
            "max_lag_seconds": max_lag,
            "quality_reason": quality,
            "usable_for_current_state": quality == "recent_observations",
            "source_updated_at": technical.get("source_updated_at"),
            "fetch_status": technical.get("status"),
            "fetch_lag_seconds": technical.get("fetch_lag_seconds"),
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


CURRENT_STATE_LABELS = {
    "heart_rate_bpm": "Пульс",
    "stress_score": "Стресс",
    "body_battery": "Body Battery",
    "respiration_rpm": "Дыхание",
    "spo2_pct": "SpO₂",
}


def _lag_text(seconds):
    if seconds is None:
        return None
    minutes = max(0, int(seconds) // 60)
    if minutes == 0:
        return "меньше минуты"
    days, remainder = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes and not days:
        parts.append(f"{minutes} мин")
    return " ".join(parts)


def _observation_time(value, now, zone):
    if not value:
        return None
    observed = datetime.fromisoformat(value).astimezone(zone)
    current = now.astimezone(zone)
    if observed.date() == current.date():
        return observed.strftime("%H:%M")
    if observed.year == current.year:
        return observed.strftime("%d.%m %H:%M")
    return observed.strftime("%d.%m.%Y %H:%M")


def render_current_state_freshness(channels, now, timezone):
    """Render verified current-state recency without asking the model to restate numbers."""
    zone = ZoneInfo(timezone)
    lines = []
    reasons = {
        "recent_observations": "данных достаточно для оценки текущего состояния",
        "stale_observation": "новых измерений пока нет",
        "partial": "свежие точки есть, но в недавнем периоде есть пробелы",
        "source_empty": "последнее обновление не содержало измерений",
        "parser_error": "данные получены, но не обработаны",
        "fetch_error": "последняя загрузка не удалась",
        "not_synced": "данные ещё не синхронизированы",
        "unknown": "свежесть данных не подтверждена",
    }
    for metric, label in CURRENT_STATE_LABELS.items():
        channel = channels.get(metric)
        if not channel:
            continue
        observed = _observation_time(channel.get("newest_observed_at"), now, zone)
        lag = _lag_text(channel.get("observation_lag_seconds"))
        if observed and lag:
            detail = f"последняя точка в {observed} ({lag} назад)"
        elif observed:
            detail = f"последняя точка в {observed}"
        else:
            detail = "измерений пока нет"
        reason = reasons.get(channel.get("quality_reason"), "свежесть данных не подтверждена")
        if channel.get("refresh_mode") == "daily":
            reason = f"суточное обновление, не показатель реального времени; {reason}"
        lines.append(f"— {label}: {detail}; {reason}.")
    if not lines:
        return ""
    frequent_without_new_data = [
        channel
        for metric in CURRENT_STATE_LABELS
        if (channel := channels.get(metric))
        and channel.get("refresh_mode") == "frequent"
        and not channel.get("usable_for_current_state", False)
        and channel.get("quality_reason") in {"stale_observation", "source_empty"}
    ]
    all_frequent_unavailable = [
        channel
        for metric in CURRENT_STATE_LABELS
        if (channel := channels.get(metric))
        and channel.get("refresh_mode") == "frequent"
        and not channel.get("usable_for_current_state", False)
    ]
    checked_recently = (
        frequent_without_new_data
        and len(frequent_without_new_data) == len(all_frequent_unavailable)
        and all(
            channel.get("fetch_status") not in {None, "error", "fetch_error"}
            and channel.get("fetch_lag_seconds") is not None
            and channel["fetch_lag_seconds"] <= 1800
            for channel in frequent_without_new_data
        )
    )
    heading = ["Актуальность показателей на момент ответа:"]
    if checked_recently:
        heading.append("Garmin проверен недавно, но более новых измерений не вернул.")
    return "\n".join([*heading, *lines])


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
        "parser_status": row.status,
    }
