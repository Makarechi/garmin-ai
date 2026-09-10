"""Bounded sleep analysis using recorded summaries and explicitly reported naps."""

import json
import math
from datetime import UTC, date, datetime, time, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from sqlalchemy import Date, cast, func, select

from garmin_ai.models import Event, HealthDay, TimelineInterval
from garmin_ai.queries import date_range

FIELDS = (
    "sleep_seconds",
    "deep_seconds",
    "rem_seconds",
    "light_seconds",
    "awake_seconds",
    "sleep_score",
)


def regularity_minutes(times):
    if len(times) < 3:
        return None
    angles = [2 * math.pi * (t.hour * 60 + t.minute + t.second / 60) / 1440 for t in times]
    strength = min(
        1, math.hypot(mean(math.cos(a) for a in angles), mean(math.sin(a) for a in angles))
    )
    return 1440 / (2 * math.pi) * math.sqrt(-2 * math.log(strength)) if strength > 1e-9 else None


def sleep_analysis(session, start: date, end: date, timezone: str, nap_policy="separate"):
    date_range(start, end, maximum=30)
    if nap_policy not in {"separate", "include_confirmed"}:
        raise ValueError("Unknown nap policy")
    zone = ZoneInfo(timezone)
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    summaries = {
        row.day: row
        for row in session.scalars(select(HealthDay).where(HealthDay.day.between(start, end)))
    }
    nights = {
        row.id: row
        for row in session.scalars(
            select(TimelineInterval).where(
                TimelineInterval.id.in_([f"sleep:{day}" for day in dates])
            )
        )
    }
    left, right = (
        datetime.combine(start - timedelta(days=1), time.min, UTC),
        datetime.combine(end + timedelta(days=2), time.min, UTC),
    )
    naps = session.scalars(
        select(Event)
        .where(
            Event.kind == "nap",
            Event.deleted.is_(False),
            Event.status == "confirmed",
            Event.source != "inferred",
            Event.start >= left,
            Event.start < right,
            cast(func.timezone(Event.timezone, Event.start), Date).between(start, end),
        )
        .order_by(Event.start, Event.id)
        .limit(201)
    ).all()
    if len(naps) > 200:
        raise ValueError("Sleep analysis exceeds 200 naps; narrow the interval")
    rows, bedtimes, wake_times = [], [], []
    for day in dates:
        summary = summaries.get(day)
        values = {field: getattr(summary, field) if summary else None for field in FIELDS}
        source_refs = {
            field: summary.sources.get(f"field:{field}") if summary else None for field in FIELDS
        }
        night = nights.get(f"sleep:{day}")
        if night:
            bedtimes.append(night.start.astimezone(zone))
            wake_times.append(night.end.astimezone(zone))
        selected_naps = [
            nap for nap in naps if nap.start.astimezone(ZoneInfo(nap.timezone)).date() == day
        ]
        intervals = sorted(
            (nap.start, nap.end) for nap in selected_naps if nap.end and nap.end > nap.start
        )
        overlap = bool(night and any(a < night.end and b > night.start for a, b in intervals))
        merged = []
        for a, b in intervals:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        nap_seconds = sum((b - a).total_seconds() for a, b in merged) if merged else None
        complete_nap_intervals = len(intervals) == len(selected_naps)
        documented = values["sleep_seconds"]
        if nap_policy == "include_confirmed" and selected_naps:
            documented = (
                (documented + nap_seconds)
                if documented is not None
                and nap_seconds is not None
                and night is not None
                and complete_nap_intervals
                and not overlap
                else None
            )
        denominator = values["sleep_seconds"]
        stages = [values[field] for field in ("deep_seconds", "rem_seconds", "light_seconds")]
        coherent = (
            denominator is not None
            and denominator > 0
            and all(v is not None for v in stages)
            and abs(sum(stages) - denominator) <= 60
        )
        rows.append(
            {
                "day": str(day),
                "values": values,
                "source_refs": source_refs,
                "unavailable": [field for field, value in values.items() if value is None],
                "main_interval": {
                    "start": night.start.isoformat(),
                    "end": night.end.isoformat(),
                    "source_ref": night.evidence.get("source_ref"),
                }
                if night
                else None,
                "naps": [
                    {
                        "event_id": str(nap.id),
                        "revision": nap.revision,
                        "timezone": nap.timezone,
                        "start": nap.start.isoformat(),
                        "end": nap.end.isoformat() if nap.end else None,
                    }
                    for nap in selected_naps[:10]
                ],
                "naps_total": len(selected_naps),
                "naps_truncated": len(selected_naps) > 10,
                "nap_status": "unanswered"
                if not selected_naps
                else "incomplete_interval"
                if not complete_nap_intervals
                else "main_interval_unknown"
                if night is None
                else "overlaps_main"
                if overlap
                else "reported",
                "reported_nap_seconds": nap_seconds,
                "documented_sleep_seconds": documented,
                "stage_fractions": {
                    field: values[field] / denominator
                    for field in ("deep_seconds", "rem_seconds", "light_seconds")
                }
                if coherent
                else None,
            }
        )
    documented_values = [
        row["documented_sleep_seconds"]
        for row in rows
        if row["documented_sleep_seconds"] is not None
    ]
    visible = []
    budget = 20000
    for row in rows:
        size = len(json.dumps(row, ensure_ascii=False)) + 2
        if size > budget:
            break
        visible.append(row)
        budget -= size
    return {
        "algorithm_version": "sleep-analysis-v1",
        "nap_policy": nap_policy,
        "timezone": timezone,
        "units": {field: "score" if field == "sleep_score" else "s" for field in FIELDS},
        "rows": visible,
        "rows_truncated": len(visible) < len(rows),
        "next_day": rows[len(visible)]["day"] if len(visible) < len(rows) else None,
        "summary": {
            "days": len(rows),
            "available_sleep_days": len(documented_values),
            "mean_documented_sleep_seconds": mean(documented_values) if documented_values else None,
            "bedtime_circular_sd_minutes": regularity_minutes(bedtimes),
            "wake_time_circular_sd_minutes": regularity_minutes(wake_times),
            "timed_main_sessions": len(bedtimes),
        },
        "limitations": [
            "Garmin calendar dates identify main summaries; reported naps use their local start date.",
            "No nap report means unknown, not absence. Documented duration is not a complete daily sleep total.",
            "Stages are main-sleep duration summaries, not reconstructed stage intervals. Subjective sleep quality is not Garmin Sleep Score.",
            "Overlapping or incomplete nap intervals are not added to main sleep. Device estimates do not establish recovery or predict migraine.",
        ],
    }
