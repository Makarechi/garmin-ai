"""A single main sleep interval, independently verified against its current source."""

import hashlib
import json
from datetime import date
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from garmin_ai.models import AppState, SourcePayload, TimelineInterval
from garmin_ai.normalize import PARSER_VERSION, timestamp

TIMING_FENCE_WAIT_MS = 100_000


def _lock_wait_expired(error):
    return getattr(error.orig, "sqlstate", None) in {"55P03", "57014"}


def acquire_timing_fence(session):
    """Join the writer queue, without ever waiting to upgrade an owned read lock."""
    connection = session.connection()
    if connection.scalar(select(func.pg_try_advisory_xact_lock(72104619))):
        return True
    if connection.scalar(select(func.pg_current_xact_id_if_assigned())) is not None:
        return False
    if connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid() AND classid = 0 AND objid = 72104619 "
            "AND objsubid = 1 AND mode = 'ShareLock' AND granted)"
        )
    ):
        return False
    wait_ms = max(
        1, min(TIMING_FENCE_WAIT_MS, session.info.get("sleep_timing_wait_ms", TIMING_FENCE_WAIT_MS))
    )
    previous = connection.scalar(select(func.current_setting("lock_timeout")))
    try:
        # A connection savepoint does not flush ORM changes before the fence.
        with connection.begin_nested():
            connection.execute(select(func.set_config("lock_timeout", f"{wait_ms}ms", True)))
            connection.execute(select(func.pg_advisory_xact_lock(72104619)))
            connection.execute(select(func.set_config("lock_timeout", previous, True)))
    except DBAPIError as error:
        if not _lock_wait_expired(error):
            raise
        return False
    return True


def acquire_timing_delivery_fence(connection):
    """A bounded queue wait on an autocommit delivery connection."""
    previous = None
    try:
        if connection.scalar(select(func.pg_try_advisory_lock_shared(72104619))):
            return True
        previous = connection.scalar(select(func.current_setting("lock_timeout")))
        connection.execute(
            select(func.set_config("lock_timeout", f"{TIMING_FENCE_WAIT_MS}ms", False))
        )
        connection.execute(select(func.pg_advisory_lock_shared(72104619)))
    except BaseException as error:
        # A cancelled session-lock statement may have acquired the lock before
        # cancellation was delivered. Do not return a locked connection to the pool.
        if not connection.invalidated:
            try:
                connection.execute(select(func.pg_advisory_unlock_shared(72104619)))
            except BaseException:
                connection.invalidate()
                raise
        if isinstance(error, DBAPIError) and _lock_wait_expired(error):
            return False
        raise
    finally:
        if previous is not None and not connection.invalidated:
            try:
                connection.execute(select(func.set_config("lock_timeout", previous, False)))
            except BaseException:
                connection.invalidate()
                raise
    return True


def timing_result_hash(result):
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode("utf-8")).hexdigest()


def timing_projection_current(session, snapshot):
    """Recheck the same single-night evidence while the delivery fence is held."""
    if not isinstance(snapshot, dict) or set(snapshot) != {"day", "timezone", "hash"}:
        return False
    try:
        day = date.fromisoformat(snapshot["day"])
    except (TypeError, ValueError):
        return False
    if str(day) != snapshot["day"] or not isinstance(snapshot["timezone"], str):
        return False
    return (
        timing_result_hash(main_sleep_timing(session, day, snapshot["timezone"]))
        == snapshot["hash"]
    )


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
