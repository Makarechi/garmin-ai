"""Garmin source capabilities that are safe to inspect without its SDK."""

from dataclasses import dataclass

INTEGRATION_KEY = "integration:garmin"

class AuthenticationRequired(RuntimeError):
    pass


class CircuitOpen(RuntimeError):
    pass

@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    scope: str = "day"
    target: str = "archive + normalize"


ENDPOINTS = (
    Endpoint("daily", "get_stats"),
    Endpoint("steps", "get_steps_data"),
    Endpoint("heart_rate", "get_heart_rates"),
    Endpoint("sleep", "get_sleep_data"),
    Endpoint("hrv", "get_hrv_data"),
    Endpoint("stress", "get_stress_data"),
    Endpoint("body_battery", "get_body_battery"),
    Endpoint("body_battery_events", "get_body_battery_events"),
    Endpoint("respiration", "get_respiration_data"),
    Endpoint("spo2", "get_spo2_data"),
    Endpoint("readiness", "get_training_readiness"),
    Endpoint("training_status", "get_training_status"),
    Endpoint("max_metrics", "get_max_metrics"),
    Endpoint("endurance", "get_endurance_score"),
    Endpoint("hill", "get_hill_score"),
    Endpoint("hydration", "get_hydration_data"),
    Endpoint("body_composition", "get_body_composition"),
    Endpoint("intensity", "get_intensity_minutes_data"),
    Endpoint("resting_hr", "get_rhr_day"),
    Endpoint("all_day_events", "get_all_day_events", target="archive; inspect schema"),
    Endpoint("devices", "get_devices", "global", "archive"),
    Endpoint("activity", "get_activity", "activity"),
    Endpoint("activity_details", "get_activity_details", "activity"),
    Endpoint("activity_splits", "get_activity_splits", "activity"),
    Endpoint("activity_typed_splits", "get_activity_typed_splits", "activity"),
    Endpoint("activity_zones", "get_activity_hr_in_timezones", "activity"),
    Endpoint("activity_weather", "get_activity_weather", "activity"),
)
