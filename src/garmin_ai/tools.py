"""One typed read-tool surface for HTTP, Telegram's agent, and MCP."""

import inspect
from dataclasses import dataclass
from datetime import date
from typing import Literal, get_type_hints

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
    """Intraday gauges use coverage-gated time-weighted means; steps_bucket uses sum. Returns metric contract and source provenance. Hydration is a daily summary in health_snapshot, not an intraday drink event. Range is half-open."""
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
def event_definitions(
    session,
    after_key: str | None = None,
    definition_key: str | None = None,
    before_version: int | None = None,
    limit: int = 10,
):
    """Page through system, custom and retired definitions and immutable version contracts."""
    from garmin_ai.definitions import list_definitions

    if not 1 <= limit <= 50:
        raise ValueError("Definition page limit must be 1 to 50")
    rows = list_definitions(
        session,
        include_retired=True,
        after_key=after_key,
        definition_key=definition_key,
        before_version=before_version,
        limit=limit + 1,
    )
    return {
        "rows": rows[:limit],
        "next_cursor": rows[limit - 1]["key"] if len(rows) > limit else None,
    }


@read_tool
def timeline(session, start: AwareDatetime, end: AwareDatetime):
    """Overlapping sleep, activity, wellbeing, context and plan layers with evidence. Segment labels are a legacy display projection; annotations preserve overlaps. Points cover no duration; calendar plans do not prove attendance. At most 500 annotations in 31 days."""
    return queries.timeline(session, start, end)


@read_tool
def data_freshness(session):
    """Separate API fetch times, observed-signal lag, coverage and current-state quality gates."""
    return queries.data_freshness(session)


@read_tool
def insights_list(session, limit: int = 30, cursor: str | None = None):
    """Stored insights, evidence and candidate/accepted status."""
    return queries.insights_list(session, limit, cursor)


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
    """Describe runs chronologically within the requested HR range, separating activity types; missing route/weather/sensor/RPE evidence prevents physiological ranking."""
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


class ReplayUnavailable(ValueError):
    """Health projections are temporarily unavailable during archive replay."""


MODEL_HEALTH_PACKS = ("general_diary", "sleep", "training", "wellbeing")
MODEL_PACK_TOOLS = {
    "health_snapshot": MODEL_HEALTH_PACKS,
    "health_range": MODEL_HEALTH_PACKS,
    "timeline": MODEL_HEALTH_PACKS,
    "insights_list": ("sleep", "wellbeing", "migraine"),
    "activities": ("training",),
    "activity_details": ("training",),
    "device_history": ("training",),
    "analysis_coffee_sleep": ("caffeine", "sleep"),
    "analysis_migraine_windows": ("migraine",),
    "analysis_running_efficiency": ("training", "wellbeing"),
    "analysis_sleep": ("sleep",),
    "wellbeing_observations": ("wellbeing",),
}


def model_metric_packs(metric: str) -> set[str]:
    metric = metric.removeprefix("system.")
    if metric in {"hydration_ml"}:
        return {"general_diary"}
    if metric.startswith(("sleep_", "deep_", "rem_", "light_", "awake_")):
        return {"sleep"}
    if metric in {
        "steps",
        "steps_bucket",
        "active_calories",
        "intensity_minutes",
        "training_status",
        "training_readiness_score",
        "recovery_time_minutes",
    }:
        return {"training"}
    if metric in {
        "heart_rate_bpm",
        "spo2_pct",
        "respiration_rpm",
        "resting_hr",
    } or metric.startswith(("hrv_", "stress_", "body_battery_")):
        return {"wellbeing"}
    # Unknown metrics may be backed by any source. Keep new catalog entries private
    # until their pack association is defined.
    return set(MODEL_HEALTH_PACKS)


def model_freshness(session, result):
    from garmin_ai.scenario_packs import pack_enabled

    channels = {
        metric: channel
        for metric, channel in result.get("channels", {}).items()
        if all(pack_enabled(session, pack, "llm") for pack in model_metric_packs(metric))
    }
    return {
        "checked_at": result.get("checked_at"),
        "available": bool(channels),
        "channels": channels,
        "limitations": result.get("limitations", []),
    }


def call_tool(session, name: str, arguments: dict, *, for_model=False):
    if name not in TOOLS:
        raise ValueError("Unknown read tool")
    from garmin_ai.access import TOOL_SCOPES

    if name != "data_freshness" and "read:health" in TOOL_SCOPES.get(name, set()):
        from sqlalchemy import func, select

        from garmin_ai.replay import REPLAY_NOTICE, replay_pending_condition

        session.execute(select(func.pg_advisory_xact_lock_shared(72104619)))
        if session.scalar(select(replay_pending_condition())):
            raise ReplayUnavailable(REPLAY_NOTICE)
    tool = TOOLS[name]
    validated = tool.arguments.model_validate(arguments)
    for_model = for_model or bool(session.info.get("llm_access"))
    if for_model:
        from garmin_ai.scenario_packs import event_pack, pack_enabled

        packs = set(MODEL_PACK_TOOLS.get(name, ()))
        if name == "analysis_event_windows":
            event_data_pack = event_pack(validated.event_type.removeprefix("system."))
            if event_data_pack is not None:
                packs.add(event_data_pack)
        metrics = {
            "metric_series": ("metric",),
            "personal_baseline": ("metric",),
            "analysis_compare_periods": ("metric",),
            "analysis_event_windows": ("metric",),
            "analysis_migraine_windows": ("metric",),
            "analysis_lagged_association": ("metric_a", "metric_b"),
        }.get(name, ())
        for field in metrics:
            packs.update(model_metric_packs(getattr(validated, field)))
        for pack in sorted(packs):
            if not pack_enabled(session, pack, "llm"):
                raise PermissionError(f"The {pack} scenario pack is not available to the model")
    previous = session.info.get("llm_access")
    if for_model:
        session.info["llm_access"] = True
    try:
        result = tool.fn(session, **dict(validated))
        if for_model and name == "data_freshness":
            return model_freshness(session, result)
        return result
    finally:
        if for_model:
            if previous is None:
                session.info.pop("llm_access", None)
            else:
                session.info["llm_access"] = previous


@read_tool
def analysis_sleep(
    session,
    start: date,
    end: date,
    nap_policy: Literal["separate", "include_confirmed"] = "separate",
):
    """Main sleep stages, timing regularity and explicit nap policy for at most 31 days. Missing nap logs are unknown; no recovery or prediction claims."""
    from garmin_ai.config import Settings
    from garmin_ai.sleep_analysis import sleep_analysis

    return sleep_analysis(
        session, start, end, session.info.get("timezone") or Settings().timezone, nap_policy
    )


@read_tool
def wellbeing_observations(
    session, start: AwareDatetime, end: AwareDatetime, cursor: str | None = None
):
    """Reported energy, restedness, pain and daily-function impact, independent of Garmin scores. At most 31 days and 200 reports per bounded page; pass next_cursor as cursor with the same range to continue. Missing values remain unknown."""
    from garmin_ai.wellbeing import observations

    return observations(session, start, end, cursor)


@read_tool
def analysis_coffee_sleep(
    session,
    start: date,
    end: date,
    late_hours: float = 6,
    outcome: Literal["sleep_score", "sleep_seconds"] = "sleep_score",
):
    """Compare caffeine timing and main sleep over at most 31 inclusive calendar dates using explicit diary coverage, exclusions and versioned evidence; missing diary is never zero intake."""
    from garmin_ai.coffee_sleep import analyze

    return analyze(session, start, end, late_hours, outcome)


@read_tool
def device_history(session, start: AwareDatetime, end: AwareDatetime, limit: int = 100):
    """Bounded activity-scoped FIT device and historical zone evidence; no serial numbers or inferred sensor attribution."""
    from garmin_ai.device_history import history

    return history(session, start, end, limit)
