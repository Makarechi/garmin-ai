"""Explicit capabilities for shared read tools; new tools require an explicit policy."""

TOOL_SCOPES = {
    "health_snapshot": {"read:health"},
    "health_range": {"read:health"},
    "metric_series": {"read:health"},
    "activities": {"read:health"},
    "activity_details": {"read:health"},
    "events": {"read:diary"},
    "wellbeing_observations": {"read:diary"},
    "timeline": {"read:health", "read:diary"},
    "data_freshness": {"read:health"},
    "insights_list": {"read:health", "read:diary"},
    "personal_baseline": {"read:health"},
    "analysis_compare_periods": {"read:health"},
    "analysis_running_efficiency": {"read:health"},
    "analysis_event_windows": {"read:health", "read:diary"},
    "analysis_migraine_windows": {"read:health", "read:diary"},
    "analysis_lagged_association": {"read:health"},
    "analysis_sleep": {"read:health", "read:diary"},
}


def permits(granted, required):
    return "admin" in granted or required <= granted


def permits_tool(granted, name):
    return name in TOOL_SCOPES and permits(granted, TOOL_SCOPES[name])
