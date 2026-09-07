"""One typed read-tool surface for HTTP, Telegram's agent, and MCP."""

import inspect
from dataclasses import dataclass
from datetime import date
from typing import get_type_hints

from pydantic import AwareDatetime, ConfigDict, create_model

from garmin_ai import analytics, queries


@dataclass
class Tool:
    name: str
    description: str
    fn: object
    arguments: object


TOOLS: dict[str, Tool] = {}


def read_tool(fn):
    hints = get_type_hints(fn)
    fields = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name == "session":
            continue
        fields[name] = (
            hints[name],
            ... if param.default is inspect.Parameter.empty else param.default,
        )
    model = create_model(
        fn.__name__ + "Args", __config__=ConfigDict(extra="forbid", allow_inf_nan=False), **fields
    )
    TOOLS[fn.__name__] = Tool(fn.__name__, fn.__doc__ or fn.__name__, fn, model)
    return fn


@read_tool
def health_snapshot(session, day: date):
    """Daily Garmin values and explicit missing metrics for one local date."""
    return queries.health_snapshot(session, day)


@read_tool
def health_range(session, start: date, end: date):
    """Stored daily summaries for an inclusive date range."""
    return queries.health_range(session, start, end)


@read_tool
def metric_series(session, metric: str, start: AwareDatetime, end: AwareDatetime, minutes: int = 5):
    """Intraday heart_rate_bpm, stress_score, body_battery, spo2_pct, respiration_rpm, steps_bucket hrv_rmssd_ms or hydration_ml; range is half-open."""
    return queries.metric_series(session, metric, start, end, minutes)


@read_tool
def activities(session, start: AwareDatetime, end: AwareDatetime, kind: str | None = None):
    """List stored activity summaries in a half-open timestamp range."""
    return queries.list_activities(session, start, end, kind)


@read_tool
def activity_details(
    session, activity_id: str, include_samples: bool = False, offset: int = 0, limit: int = 100
):
    """Details, laps and HR zones for one activity. Raw samples are excluded by default. Use next_offset to continue and a smaller limit for bulky samples."""
    return queries.activity_details(session, activity_id, include_samples, offset, limit)


@read_tool
def events(session, start: AwareDatetime, end: AwareDatetime, kind: str | None = None):
    """Read confirmed, inferred, or pending diary events with their status; excludes deleted records."""
    return queries.list_events(session, start, end, kind)


@read_tool
def timeline(session, start: AwareDatetime, end: AwareDatetime):
    """Known, inferred, and explicitly unknown intervals. Never infers meetings or driving from heart rate."""
    return queries.timeline(session, start, end)


@read_tool
def data_freshness(session):
    """Last successful fetch by endpoint and current lag; missing entries mean unverified freshness."""
    return queries.data_freshness(session)


@read_tool
def insights_list(session, limit: int = 30):
    """Stored insights, evidence and candidate/accepted status."""
    return queries.insights_list(session, limit)


@read_tool
def personal_baseline(session, metric: str, start: date, end: date):
    """Compute a personal baseline from available daily values with sample size and missing days."""
    return analytics.personal_baseline(session, metric, start, end)


@read_tool
def analysis_compare_periods(
    session, metric: str, a_start: date, a_end: date, b_start: date, b_end: date
):
    """Compare non-overlapping daily periods with block-bootstrap uncertainty when enough observations exist."""
    return analytics.compare_periods(session, metric, a_start, a_end, b_start, b_end)


@read_tool
def analysis_running_efficiency(
    session, start: AwareDatetime, end: AwareDatetime, hr_min: float = 0, hr_max: float = 250
):
    """Rank runs by meters per heartbeat within the requested HR range; report sleep/HRV context and terrain limitations."""
    return analytics.running_efficiency(session, start, end, hr_min, hr_max)


@read_tool
def analysis_event_windows(
    session, event_type: str, metric: str, start: AwareDatetime, end: AwareDatetime
):
    """Compute measured physiology in -48h through +24h windows around diary events."""
    return analytics.event_windows(session, event_type, metric, start, end)


@read_tool
def analysis_migraine_windows(
    session, metric: str, start: date, end: date, timezone: str | None = None
):
    """Match logged migraine days to weekday controls with explicit limitations and sample sizes."""
    from garmin_ai.config import Settings

    return analytics.migraine_comparison(
        session, metric, start, end, timezone or session.info.get("timezone") or Settings().timezone
    )


@read_tool
def analysis_lagged_association(
    session, metric_a: str, metric_b: str, start: date, end: date, lags: list[int]
):
    """Exploratory daily correlations at specified calendar-day lags; association is not causation."""
    return analytics.lagged_association(session, metric_a, metric_b, start, end, lags)


def call_tool(session, name: str, arguments: dict):
    if name not in TOOLS:
        raise ValueError("Unknown read tool")
    tool = TOOLS[name]
    validated = tool.arguments.model_validate(arguments)
    return tool.fn(session, **dict(validated))
