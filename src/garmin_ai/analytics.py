"""Reproducible descriptive analyses with explicit denominators and limitations."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from scipy.optimize import linear_sum_assignment
from sqlalchemy import select

from garmin_ai.models import Activity, Event, HealthDay, Measurement
from garmin_ai.queries import (
    EVENT_KINDS,
    HEALTH_METRICS,
    MEASUREMENT_METRICS,
    date_range,
    time_range,
)
from garmin_ai.temporal import feature_at


def describe(values):
    x = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if not len(x):
        return {"n": 0, "mean": None, "median": None, "sd": None, "min": None, "max": None}
    return {
        "n": len(x),
        "mean": float(x.mean()),
        "median": float(np.median(x)),
        "sd": float(x.std(ddof=1)) if len(x) > 1 else None,
        "min": float(x.min()),
        "max": float(x.max()),
    }


def block_mean_difference(a, b, seed=42):
    """Seven-observation circular blocks reduce the independence assumption."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if min(len(a), len(b)) < 14:
        return {
            "difference": float(a.mean() - b.mean()) if len(a) and len(b) else None,
            "ci95": None,
            "method": "insufficient observations for block resampling",
        }
    rng = np.random.default_rng(seed)

    def resample(x):
        starts = rng.integers(0, len(x), size=(1000, int(np.ceil(len(x) / 7))))
        indices = (starts[:, :, None] + np.arange(7)) % len(x)
        return x[indices.reshape(1000, -1)[:, : len(x)]].mean(axis=1)

    distribution = resample(a) - resample(b)
    return {
        "difference": float(a.mean() - b.mean()),
        "ci95": [float(v) for v in np.quantile(distribution, [0.025, 0.975])],
        "method": "7-observation circular block bootstrap; 1000 resamples; seed 42",
    }


def personal_baseline(session, metric: str, start: date, end: date):
    date_range(start, end)
    if metric not in HEALTH_METRICS:
        raise ValueError("Unknown daily metric")
    rows = session.execute(
        select(HealthDay.day, getattr(HealthDay, metric))
        .where(HealthDay.day.between(start, end))
        .order_by(HealthDay.day)
    ).all()
    valid = [(d, v) for d, v in rows if v is not None]
    return {
        "metric": metric,
        "start": str(start),
        "end": str(end),
        **describe([v for _, v in valid]),
        "observed_dates": [str(d) for d, _ in valid],
        "missing_days": (end - start).days + 1 - len(valid),
        "method": "observed daily values; missing days are not zero",
    }


def compare_periods(session, metric: str, a_start: date, a_end: date, b_start: date, b_end: date):
    if max(a_start, b_start) <= min(a_end, b_end):
        raise ValueError("Comparison periods must not overlap")
    a = personal_baseline(session, metric, a_start, a_end)
    b = personal_baseline(session, metric, b_start, b_end)
    column = getattr(HealthDay, metric)
    av = session.scalars(
        select(column)
        .where(HealthDay.day.between(a_start, a_end), column.is_not(None))
        .order_by(HealthDay.day)
    ).all()
    bv = session.scalars(
        select(column)
        .where(HealthDay.day.between(b_start, b_end), column.is_not(None))
        .order_by(HealthDay.day)
    ).all()
    result = block_mean_difference(av, bv)
    pooled = None
    if a["sd"] is not None and b["sd"] is not None:
        pooled = np.sqrt(
            ((a["n"] - 1) * a["sd"] ** 2 + (b["n"] - 1) * b["sd"] ** 2) / (a["n"] + b["n"] - 2)
        )
    effect = (
        result["difference"] / pooled
        if result["difference"] is not None and pooled is not None and pooled > 0
        else None
    )
    return {
        "metric": metric,
        "a": a,
        "b": b,
        **result,
        "standardized_difference": effect,
        "limitations": [
            "Observational association; not evidence of causation",
            "Missing days can bias estimates; blocks follow observations, not calendar gaps",
            "No adjustment for multiple comparisons or unmeasured confounders",
        ],
    }


def running_efficiency(
    session, start: datetime, end: datetime, hr_min: float = 0, hr_max: float = 250
):
    time_range(start, end, 3660)
    if not 0 <= hr_min < hr_max <= 300:
        raise ValueError("Invalid heart-rate range")
    activities = session.scalars(
        select(Activity)
        .where(
            Activity.start >= start,
            Activity.start < end,
            Activity.kind.in_(
                [
                    "running",
                    "trail_running",
                    "treadmill_running",
                    "track_running",
                    "indoor_running",
                    "ultra_run",
                    "virtual_run",
                    "obstacle_run",
                ]
            ),
        )
        .order_by(Activity.start)
    ).all()
    knowledge_cutoff = datetime.now(UTC)
    rows = []
    excluded = 0
    for a in activities:
        seconds = a.moving_seconds or a.duration_seconds
        if (
            not seconds
            or seconds < 1200
            or not a.distance_m
            or not a.avg_hr
            or not hr_min <= a.avg_hr <= hr_max
        ):
            excluded += 1
            continue
        context = {
            metric: feature_at(session, metric, a.start, knowledge_cutoff)
            for metric in ("training_readiness_score", "sleep_score", "hrv_nightly_avg")
        }
        rows.append(
            {
                "activity_id": a.id,
                "start": a.start.isoformat(),
                "kind": a.kind,
                "distance_m": a.distance_m,
                "avg_hr": a.avg_hr,
                "pace_seconds_per_km": seconds / a.distance_m * 1000,
                "meters_per_heartbeat": a.distance_m / seconds * 60 / a.avg_hr,
                "ascent_m_per_km": a.ascent_m / a.distance_m * 1000
                if a.ascent_m is not None
                else None,
                "sleep_score": context["sleep_score"]["value"],
                "hrv_nightly_avg": context["hrv_nightly_avg"]["value"],
                "readiness": context["training_readiness_score"]["value"],
                "context": context,
            }
        )
    rows.sort(key=lambda r: r["meters_per_heartbeat"], reverse=True)
    return {
        "n": len(rows),
        "excluded": excluded,
        "rows": rows,
        "hr_range": [hr_min, hr_max],
        "method": "distance / moving duration * 60 / average HR; minimum 20 minutes",
        "limitations": [
            "Descriptive ranking, not grade or weather adjusted",
            "Compare similar terrain and activity type; mixed conditions are shown explicitly",
            "Whole-activity means do not establish steady-state cardiac efficiency",
            "Pre-event context requires source time; calendar-only HRV is unknown",
        ],
    }


def event_windows(session, event_type: str, metric: str, start: datetime, end: datetime):
    time_range(start, end, 366)
    if event_type not in EVENT_KINDS:
        raise ValueError("Unknown event type")
    if metric not in MEASUREMENT_METRICS:
        raise ValueError("Unknown measurement metric")
    events = session.scalars(
        select(Event)
        .where(
            Event.kind == event_type,
            Event.deleted.is_(False),
            Event.status == "confirmed",
            Event.start >= start,
            Event.start < end,
        )
        .order_by(Event.start)
        .limit(101)
    ).all()
    if len(events) > 100:
        raise ValueError("Limit analysis to at most 100 episodes")
    rows = []
    for e in events:
        windows = []
        periods = [(-48, -24), (-24, -12), (-12, -6), (-6, 0), (0, 24)]
        for low, high in periods:
            left = e.start + timedelta(hours=low)
            right = e.start + timedelta(hours=high)
            values = session.scalars(
                select(Measurement.value).where(
                    Measurement.metric == metric, Measurement.ts >= left, Measurement.ts < right
                )
            ).all()
            windows.append(
                {
                    "relative_hours": [low, high],
                    "start": left.isoformat(),
                    "end": right.isoformat(),
                    **describe(values),
                }
            )
        rows.append({"event_id": str(e.id), "start": e.start.isoformat(), "windows": windows})
    return {
        "event_type": event_type,
        "metric": metric,
        "episodes": len(rows),
        "rows": rows,
        "limitations": [
            "Samples are correlated and unevenly spaced; means are descriptive, not time-weighted",
            "No event or no samples is missing evidence, never evidence of absence",
        ],
    }


def migraine_comparison(session, metric: str, start: date, end: date, timezone="Europe/Bratislava"):
    date_range(start, end)
    if start < date.min + timedelta(days=60) or end > date.max - timedelta(days=60):
        raise ValueError("Dates must allow expansion of control windows")
    if metric not in HEALTH_METRICS:
        raise ValueError("Unknown daily metric")
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError("Unknown timezone") from None
    left = datetime.combine(start, datetime.min.time(), zone)
    right = datetime.combine(end + timedelta(days=1), datetime.min.time(), ZoneInfo(timezone))
    episodes = session.scalars(
        select(Event).where(
            Event.kind == "migraine",
            Event.deleted.is_(False),
            Event.status == "confirmed",
            Event.start >= left - timedelta(days=59),
            Event.start < right + timedelta(days=59),
        )
    ).all()
    migraine_days = {e.start.astimezone(ZoneInfo(timezone)).date() for e in episodes}
    days = {
        h.day: h
        for h in session.scalars(
            select(HealthDay).where(
                HealthDay.day.between(start - timedelta(days=56), end + timedelta(days=56))
            )
        )
    }
    observed_episodes = sorted(
        d
        for d in migraine_days
        if start <= d <= end and d in days and getattr(days[d], metric) is not None
    )
    if len(observed_episodes) > 200:
        raise ValueError(
            "Limit migraine comparison to at most 200 observed episode days; narrow the date range"
        )
    controls = sorted(
        d
        for d in days
        if all(abs((d - m).days) > 3 for m in migraine_days)
        and getattr(days[d], metric) is not None
    )
    pairs = []
    if observed_episodes and controls:
        penalty = (len(observed_episodes) + 1) * 57
        costs = np.full(
            (len(observed_episodes), len(controls) + len(observed_episodes)), float(penalty)
        )
        costs[:, : len(controls)] = penalty * 2
        for i, day in enumerate(observed_episodes):
            for j, control in enumerate(controls):
                distance = abs((control - day).days)
                if day.weekday() == control.weekday() and 0 < distance <= 56:
                    costs[i, j] = distance + j * 1e-7
        row_indices, column_indices = linear_sum_assignment(costs)
        for i, j in zip(row_indices, column_indices, strict=True):
            if j >= len(controls) or costs[i, j] >= penalty:
                continue
            day, control = observed_episodes[i], controls[j]
            pairs.append(
                {
                    "event_day": str(day),
                    "control_day": str(control),
                    "event_value": getattr(days[day], metric),
                    "control_value": getattr(days[control], metric),
                }
            )
    differences = [p["event_value"] - p["control_value"] for p in pairs]
    ci = None
    p_value = None
    if len(pairs) >= 10:
        rng = np.random.default_rng(42)
        x = np.asarray(differences)
        ci = [
            float(v)
            for v in np.quantile(
                rng.choice(x, size=(2000, len(x)), replace=True).mean(axis=1), [0.025, 0.975]
            )
        ]
        null = (rng.choice([-1, 1], size=(2000, len(x))) * x).mean(axis=1)
        p_value = float((np.sum(abs(null) >= abs(x.mean())) + 1) / 2001)
    return {
        "metric": metric,
        "episodes": sum(start <= d <= end for d in migraine_days),
        "matched_pairs": len(pairs),
        "pairs": pairs,
        "difference": describe(differences),
        "ci95": ci,
        "exploratory_sign_permutation_p": p_value,
        "method": "maximum-cardinality same-weekday control matching within 56 days, then minimum total distance; controls exclude +/-3 days around migraine starts; no control reuse",
        "limitations": [
            "Only logged migraine starts are known; unlogged episodes may contaminate controls",
            "Unadjusted for medication, sleep, training, alcohol, weather, or other confounders",
            "Intervals require at least 10 pairs; serial dependence and multiple testing limit inference",
            "Association only; cannot establish causes or treatment effects",
        ],
    }


def lagged_association(
    session, metric_a: str, metric_b: str, start: date, end: date, lags: list[int]
):
    date_range(start, end)
    if (
        metric_a not in HEALTH_METRICS
        or metric_b not in HEALTH_METRICS
        or len(lags) > 15
        or any(abs(lag) > 30 for lag in lags)
    ):
        raise ValueError("Invalid metric or lag range")
    rows = {
        r.day: r
        for r in session.scalars(select(HealthDay).where(HealthDay.day.between(start, end)))
    }
    results = []
    for lag in lags:
        pairs = [
            (getattr(r, metric_a), getattr(rows[d + timedelta(days=lag)], metric_b))
            for d, r in rows.items()
            if d + timedelta(days=lag) in rows
        ]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        correlation = None
        if len(pairs) >= 10:
            a, b = np.asarray(pairs).T
            if a.std() > 0 and b.std() > 0:
                correlation = float(np.corrcoef(a, b)[0, 1])
        results.append({"lag_days": lag, "n": len(pairs), "pearson_r": correlation})
    return {
        "a": metric_a,
        "b": metric_b,
        "results": results,
        "limitations": [
            "Exploratory correlations; multiple comparisons and serial dependence are not adjusted",
            "A lag does not establish causation",
        ],
    }
