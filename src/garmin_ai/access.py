"""Explicit capabilities for shared read tools; new tools require an explicit policy."""

TOOL_SCOPES = {
    "analysis_coffee_sleep": {"read:health", "read:diary"},
    "device_history": {"read:health"},
    "health_snapshot": {"read:health"},
    "health_range": {"read:health"},
    "metric_series": {"read:health"},
    "activities": {"read:health"},
    "activity_details": {"read:health"},
    "events": {"read:diary"},
    "event_definitions": {"read:diary"},
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
    "generic_analysis": {"read:health", "read:diary"},
}


def permits(granted, required):
    return "admin" in granted or required <= granted


def required_tool_scopes(name, validated=None):
    if name != "generic_analysis" or validated is None:
        return TOOL_SCOPES.get(name)
    spec = validated.spec
    if spec.operation == "query_entries" or (spec.metric_key or "").startswith("user."):
        return {"read:diary"}
    return {"read:health"}


def permits_tool(granted, name, validated=None):
    required = required_tool_scopes(name, validated)
    if required is None:
        return False
    if name == "generic_analysis" and validated is None:
        return "admin" in granted or bool({"read:health", "read:diary"} & granted)
    return permits(granted, required)
