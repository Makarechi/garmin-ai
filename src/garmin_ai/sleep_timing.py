"""A single main sleep interval, independently verified against its current source."""

from uuid import UUID

from garmin_ai.models import AppState, SourcePayload, TimelineInterval
from garmin_ai.normalize import PARSER_VERSION, timestamp


def main_sleep_timing(session, day, timezone):
    result = {
        "algorithm_version": "main-sleep-timing-v1",
        "timezone": timezone,
        "status": "unavailable",
        "rows": [{"day": str(day), "main_interval": None}],
        "limitations": ["Garmin's recorded main sleep interval, not proven sleep onset."],
    }
    night = session.get(TimelineInterval, f"sleep:{day}", populate_existing=True)
    if night is None:
        return result
    result["status"] = "unverified_source"
    try:
        identity = UUID(night.evidence["source_ref"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return result
    raw = session.get(SourcePayload, identity, populate_existing=True)
    if (
        raw is None
        or raw.source != "garmin_connect"
        or raw.endpoint != "sleep"
        or raw.source_key != str(day)
        or raw.parser_version != PARSER_VERSION
        or raw.status != "normalized"
        or night.source != "garmin_connect"
        or night.label != "sleep"
        or not night.confirmed
    ):
        return result
    state = session.get(AppState, f"ingest:{raw.source}:sleep:{day}", populate_existing=True)
    state_value = state.value if state is not None and isinstance(state.value, dict) else {}
    attempt = state_value.get("latest_attempt", {})
    if (
        state_value.get("source_ref") != str(raw.id)
        or state_value.get("hash") != raw.payload_hash
        or state_value.get("status") not in ("normalized", "empty")
        or not isinstance(attempt, dict)
        or attempt.get("status") in ("error", "pending")
    ):
        return result
    metadata = session.get(AppState, f"ingest-meta:{raw.id}", populate_existing=True)
    if metadata and (
        not isinstance(metadata.value, dict)
        or metadata.value.get("failed_parser_version") == PARSER_VERSION
    ):
        return result
    dto = raw.payload.get("dailySleepDTO") if isinstance(raw.payload, dict) else None
    if not isinstance(dto, dict):
        return result
    if dto.get("calendarDate") not in (None, str(day)):
        return result
    if any(
        type(dto.get(field)) is bool for field in ("sleepStartTimestampGMT", "sleepEndTimestampGMT")
    ):
        return result
    try:
        start = timestamp(dto["sleepStartTimestampGMT"])
        end = timestamp(dto["sleepEndTimestampGMT"])
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return result
    if start != night.start or end != night.end or end <= start:
        return result
    result["status"] = "available"
    result["rows"][0]["main_interval"] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source_ref": str(raw.id),
    }
    return result
