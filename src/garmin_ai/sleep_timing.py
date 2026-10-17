"""A single main sleep interval, independently verified against its current source."""

import hashlib
import json
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import aliased

from garmin_ai.models import AppState, Person, SourcePayload, TimelineInterval
from garmin_ai.normalize import PARSER_VERSION, timestamp


def timing_result_hash(result):
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode("utf-8")).hexdigest()


def timing_projection_current(session, snapshot, *, for_model=False):
    """Recheck a committed timing snapshot while the delivery policy fence is held."""
    if not isinstance(snapshot, dict) or set(snapshot) != {"day", "timezone", "hash"}:
        return False
    try:
        day = date.fromisoformat(snapshot["day"])
    except (TypeError, ValueError):
        return False
    if str(day) != snapshot["day"] or not isinstance(snapshot["timezone"], str):
        return False
    if for_model:
        from garmin_ai.tools import call_tool

        if session.scalar(select(Person.timezone).limit(1)) != snapshot["timezone"]:
            return False
        previous_timezone = session.info.get("timezone")
        session.info["timezone"] = snapshot["timezone"]
        try:
            result = call_tool(
                session,
                "analysis_sleep",
                {
                    "start": str(day),
                    "end": str(day),
                    "timing_only": True,
                },
                for_model=True,
            )
        except ValueError:
            return False
        finally:
            if previous_timezone is None:
                session.info.pop("timezone", None)
            else:
                session.info["timezone"] = previous_timezone
    else:
        result = main_sleep_timing(session, day, snapshot["timezone"])
    return timing_result_hash(result) == snapshot["hash"]


def main_sleep_timing(session, day, timezone):
    result = {
        "algorithm_version": "main-sleep-timing-v1",
        "timezone": timezone,
        "status": "unavailable",
        "rows": [{"day": str(day), "main_interval": None}],
        "limitations": ["Garmin's recorded main sleep interval, not proven sleep onset."],
    }
    # Probe only the candidate identity. All accepted evidence below comes from
    # one joined statement, so replay cannot mix independently read revisions.
    candidate = session.execute(
        select(TimelineInterval.evidence).where(TimelineInterval.id == f"sleep:{day}")
    ).first()
    if candidate is None:
        return result
    result["status"] = "unverified_source"
    try:
        source_ref = candidate[0]["source_ref"]
        identity = UUID(source_ref)
    except (KeyError, TypeError, ValueError, AttributeError):
        return result
    watermark = aliased(AppState)
    metadata = aliased(AppState)
    snapshot = session.execute(
        select(SourcePayload, TimelineInterval, watermark.value, metadata.key, metadata.value)
        .select_from(SourcePayload)
        .join(TimelineInterval, TimelineInterval.id == f"sleep:{day}")
        .join(watermark, watermark.key == f"ingest:garmin_connect:sleep:{day}")
        .outerjoin(metadata, metadata.key == f"ingest-meta:{identity}")
        .where(
            SourcePayload.id == identity,
            TimelineInterval.evidence["source_ref"].astext == source_ref,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if snapshot is None:
        return result
    raw, night, state_value, metadata_key, metadata_value = snapshot
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
    state_value = state_value if isinstance(state_value, dict) else {}
    attempt = state_value.get("latest_attempt", {})
    if (
        state_value.get("source_ref") != str(raw.id)
        or state_value.get("hash") != raw.payload_hash
        or state_value.get("status") not in ("normalized", "empty")
        or not isinstance(attempt, dict)
        or attempt.get("status") in ("error", "pending")
    ):
        return result
    if metadata_key is not None and (
        not isinstance(metadata_value, dict)
        or metadata_value.get("failed_parser_version") == PARSER_VERSION
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
