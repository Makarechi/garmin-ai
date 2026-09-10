"""Canonical metric contracts shared by normalization and tool responses."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Metric:
    unit: str
    kind: str
    aggregation: str
    minimum: float
    maximum: float | None = None
    max_gap_seconds: int = 300
    interval: str = "instantaneous; no fill after last observation"
    reset: str = "not_applicable"


CATALOG = {
    "heart_rate_bpm": Metric("bpm", "gauge", "time_weighted_mean", 1, 300),
    "stress_score": Metric("score", "gauge", "time_weighted_mean", 0, 100),
    "body_battery": Metric("score", "estimate", "time_weighted_mean", 0, 100, 900),
    "spo2_pct": Metric("%", "gauge", "time_weighted_mean", 1, 100, 3600),
    "respiration_rpm": Metric("rpm", "gauge", "time_weighted_mean", 1, 100),
    "hrv_rmssd_ms": Metric("ms", "gauge", "time_weighted_mean", 0),
    "steps_bucket": Metric(
        "steps",
        "increment",
        "sum",
        0,
        interval="source interval start; increments are not cumulative",
    ),
    "hydration_ml": Metric(
        "ml",
        "daily_summary",
        "latest",
        0,
        interval="source calendar date; consumption time unknown",
    ),
}


def contract(metric):
    return {
        "metric_id": metric,
        **asdict(CATALOG[metric]),
        "quality": "observed only; finite values in canonical units",
        "provenance": "sources are kept separate; never deduplicated by value alone",
    }


def convert(value, source_unit, target_unit):
    """Explicit dimensional conversions; unsupported unit pairs fail closed."""
    factors = {
        ("ms", "s"): 0.001,
        ("minutes", "hours"): 1 / 60,
        ("m", "km"): 0.001,
        ("m/s", "km/h"): 3.6,
    }
    if source_unit == target_unit:
        if source_unit not in {u for pair in factors for u in pair} | {"s/km"} | {
            spec.unit for spec in CATALOG.values()
        }:
            raise ValueError("Unknown unit")
        return value
    if (source_unit, target_unit) in {("m/s", "s/km"), ("s/km", "m/s")}:
        return 1000 / value if value > 0 else None
    if (source_unit, target_unit) in factors:
        return value * factors[source_unit, target_unit]
    if (target_unit, source_unit) in factors:
        return value / factors[target_unit, source_unit]
    raise ValueError("Unsupported unit conversion")
