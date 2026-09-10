"""Versioned, bounded descriptive caffeine timing cohorts; no causal inference."""

import hashlib
import json
from datetime import date, timedelta

from sqlalchemy import select

from garmin_ai.analytics import block_mean_difference, describe
from garmin_ai.events import caffeine_total
from garmin_ai.models import Event, HealthDay, TimelineInterval
from garmin_ai.queries import date_range

VERSION = "coffee-sleep-v1"


def covers(intervals, left, right):
    cursor = left
    for start, end in sorted(intervals):
        if end <= cursor:
            continue
        if start > cursor:
            return False
        cursor = max(cursor, end)
        if cursor >= right:
            return True
    return False


def analyze(session, start: date, end: date, late_hours: float = 6, outcome="sleep_score"):
    date_range(start, end, maximum=30)
    if not 0 < late_hours <= 24 or outcome not in {"sleep_score", "sleep_seconds"}:
        raise ValueError("Invalid coffee/sleep specification")
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    nights = {
        row.id: row
        for row in session.scalars(
            select(TimelineInterval).where(
                TimelineInterval.id.in_([f"sleep:{day}" for day in days])
            )
        )
    }
    summaries = {
        row.day: row
        for row in session.scalars(select(HealthDay).where(HealthDay.day.between(start, end)))
    }
    intervals = [(night.start - timedelta(hours=24), night.end) for night in nights.values()]
    events = []
    if intervals:
        left, right = min(a for a, b in intervals), max(b for a, b in intervals)
        events = session.scalars(
            select(Event)
            .where(
                Event.deleted.is_(False),
                Event.status.in_(["confirmed", "needs_confirmation", "inferred"]),
                (Event.status != "confirmed") | (Event.source != "inferred"),
                Event.kind.in_(
                    ["caffeine", "caffeine_absence", "caffeine_log_complete", "illness", "travel"]
                ),
                Event.start < right,
                (Event.end >= left)
                | (Event.start >= left)
                | ((Event.kind == "illness") & Event.end.is_(None)),
            )
            .order_by(Event.start, Event.id)
            .limit(1001)
        ).all()
        if len(events) > 1000:
            raise ValueError("More than 1000 diary inputs; narrow the range")
    rows, groups = [], {"late": [], "not_late": []}
    for day in days:
        night, summary = nights.get(f"sleep:{day}"), summaries.get(day)
        value = getattr(summary, outcome) if summary else None
        row = {"day": str(day), "outcome": value, "eligible": False, "exclusions": [], "inputs": []}
        rows.append(row)
        if night is None or night.end <= night.start:
            row["exclusions"].append("missing_sleep_interval")
            continue
        left, right = night.start - timedelta(hours=24), night.start
        relevant = [
            e
            for e in events
            if e.start < (night.end if e.kind in {"illness", "travel"} else right)
            and (
                e.start >= left
                if e.end == e.start
                else e.end > left
                if e.end is not None
                else e.kind == "illness" or e.start >= left
            )
        ]
        uncertain = [
            e
            for e in relevant
            if e.status != "confirmed" and e.kind in {"caffeine", "illness", "travel"}
        ]
        confirmed = [e for e in relevant if e.status == "confirmed"]
        coffee = [e for e in confirmed if e.kind == "caffeine" and left <= e.start < right]
        absence = [e for e in confirmed if e.kind == "caffeine_absence" and e.end is not None]
        complete = [
            e
            for e in confirmed
            if e.kind in {"caffeine_absence", "caffeine_log_complete"} and e.end is not None
        ]
        covered = covers([(e.start, e.end) for e in complete], left, right)
        conflict = any(a.start <= c.start < a.end for a in absence for c in coffee)
        row.update(
            {
                "sleep_interval": {
                    "start": night.start.isoformat(),
                    "end": night.end.isoformat(),
                    "source_ref": night.evidence.get("source_ref"),
                },
                "outcome_source_ref": summary.sources.get(f"field:{outcome}") if summary else None,
                "exposure_start": left.isoformat(),
                "exposure_end": right.isoformat(),
                "diary_complete": covered,
                "coverage_conflict": conflict,
                "last_recorded_caffeine_hours_before_sleep": (
                    right - max(e.start for e in coffee)
                ).total_seconds()
                / 3600
                if coffee
                else None,
                "inputs": [
                    {
                        "event_id": str(e.id),
                        "revision": e.revision,
                        "kind": e.kind,
                        "status": e.status,
                        "start": e.start.isoformat(),
                        "end": e.end.isoformat() if e.end else None,
                        **({"dose": caffeine_total(e.payload)} if e.kind == "caffeine" else {}),
                    }
                    for e in relevant
                ],
            }
        )
        if uncertain:
            row["exclusions"].append("unconfirmed_caffeine_or_confounder")
        if not covered:
            row["exclusions"].append("incomplete_caffeine_diary")
        if conflict:
            row["exclusions"].append("contradictory_absence")
        if value is None:
            row["exclusions"].append("missing_outcome")
        if not row["outcome_source_ref"] or row["outcome_source_ref"] != night.evidence.get(
            "source_ref"
        ):
            row["exclusions"].append("inconsistent_sleep_source")
        if any(e.kind in {"illness", "travel"} for e in confirmed):
            row["exclusions"].append("recorded_illness_or_travel")
        totals = [caffeine_total(e.payload) for e in coffee]
        row["total_caffeine_mg"] = {
            key: sum(d[key] for d in totals)
            if covered
            and not conflict
            and not any(e.kind == "caffeine" for e in uncertain)
            and all(d[key] is not None for d in totals)
            else None
            for key in ("min", "estimate", "max")
        }
        if not row["exclusions"]:
            row["eligible"] = True
            row["cohort"] = (
                "late"
                if any(e.start >= right - timedelta(hours=late_hours) for e in coffee)
                else "not_late"
            )
            groups[row["cohort"]].append(value)
    spec = {
        "outcome": outcome,
        "exposure": "recorded_caffeine_in_late_window",
        "cohort_definitions": {
            "late": "At least one caffeine event in [sleep_start - late_hours, sleep_start)",
            "not_late": "No caffeine event in the late window; includes earlier caffeine and none in the complete 24-hour diary",
        },
        "late_hours": late_hours,
        "lookback_hours": 24,
        "start": str(start),
        "end": str(end),
        "unit_of_observation": "main_sleep_session",
        "minimum_per_cohort": 5,
        "method_version": VERSION,
        "exclusions": [
            "missing_interval_or_outcome",
            "inconsistent_sleep_source",
            "unconfirmed_caffeine_or_confounder",
            "incomplete_diary",
            "contradictory_absence",
            "recorded_illness_or_travel",
        ],
    }
    sufficient = min(map(len, groups.values())) >= 5
    result = {
        "spec": spec,
        "status": "exploratory" if sufficient else "insufficient_evidence",
        "rows": rows,
        "eligible": sum(row["eligible"] for row in rows),
        "excluded": sum(not row["eligible"] for row in rows),
        "cohorts": {key: describe(values) for key, values in groups.items()},
        "comparison": block_mean_difference(groups["late"], groups["not_late"])
        if sufficient
        else None,
        "limitations": [
            "Observational, not causal or a recommendation to change dose",
            "No diary record is not zero caffeine; explicit full-window coverage is required",
            "Unrecorded confounders and self-report errors remain possible",
            "Main sleep only; calendar gaps and repeated exploratory checks limit inference",
            "No confirmed replication or multiplicity adjustment",
        ],
        "next_data_needed": "Confirm completeness of caffeine records for the pre-sleep window; both timing cohorts need enough nights"
        if not sufficient
        else None,
    }
    result["evidence_hash"] = hashlib.sha256(
        json.dumps({"spec": spec, "rows": rows}, sort_keys=True).encode()
    ).hexdigest()
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 40000:
        raise ValueError("Coffee/sleep evidence exceeds budget; narrow the range")
    return result
