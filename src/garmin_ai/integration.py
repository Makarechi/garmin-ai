"""Persist Garmin connection gates without persisting upstream error text."""

import random
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from garminconnect import GarminConnectConnectionError, GarminConnectTooManyRequestsError
from sqlalchemy import text

from garmin_ai.accounts import AccountError
from garmin_ai.db import make_engine, transaction
from garmin_ai.garmin import AuthenticationRequired, CircuitOpen
from garmin_ai.models import AppState
from garmin_ai.normalize import upsert

KEY = "integration:garmin"


class IntegrationBlocked(RuntimeError):
    pass


def paused(session, now):
    row = session.get(AppState, KEY, populate_existing=True)
    if row is None:
        return False
    value = row.value
    return value.get("status") == "reauth_required" or bool(
        value.get("blocked_until") and datetime.fromisoformat(value["blocked_until"]) > now
    )


def retry_after(error, now):
    response = getattr(error, "response", None)
    value = getattr(response, "headers", {}).get("Retry-After")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(1, min(seconds, 7 * 86400)) if seconds == seconds else None


def record(session, status, now, *, delay=None, reason=None, failure=False):
    row = session.get(AppState, KEY, populate_existing=True)
    previous = row.value if row else {}
    upsert(
        session,
        AppState,
        {
            "key": KEY,
            "value": {
                "status": status,
                "blocked_until": (now + timedelta(seconds=delay)).isoformat() if delay else None,
                "last_attempt": now.isoformat(),
                "reason_class": reason,
                "failure_count": previous.get("failure_count", 0) + 1 if failure else 0,
            },
        },
        ["key"],
    )


def guarded(engine, operation, *, now=None):
    instant = now or datetime.now(UTC)
    with transaction(engine) as session:
        if paused(session, instant):
            raise IntegrationBlocked("Garmin connection is paused")
    try:
        result = operation()
    except (AuthenticationRequired, AccountError) as exc:
        instant = now or datetime.now(UTC)
        with transaction(engine) as session:
            record(session, "reauth_required", instant, reason=type(exc).__name__, failure=True)
        raise
    except (GarminConnectTooManyRequestsError, GarminConnectConnectionError, CircuitOpen) as exc:
        instant = now or datetime.now(UTC)
        with transaction(engine) as session:
            previous = session.get(AppState, KEY)
            count = previous.value.get("failure_count", 0) if previous else 0
            delay = retry_after(exc, instant) or min(
                3600, 60 * 2 ** min(count, 6)
            ) + random.uniform(0, 5)
            if isinstance(exc, CircuitOpen):
                delay = max(delay, 900)
            record(
                session,
                "rate_limited"
                if isinstance(exc, GarminConnectTooManyRequestsError)
                else "degraded",
                instant,
                delay=delay,
                reason=type(exc).__name__,
                failure=True,
            )
        raise
    with transaction(engine) as session:
        record(session, "active", instant)
    return result


def resume_after_login(settings):
    if not settings.database_url.get_secret_value():
        return
    engine = make_engine(settings)
    try:
        with engine.connect() as connection:
            if connection.scalar(text("SELECT to_regclass('app_state')")) is None:
                return
            if connection.scalar(text("SELECT 1 FROM app_state WHERE key='maintenance:erased'")):
                return
        with transaction(engine) as session:
            record(session, "active", datetime.now(UTC))
    finally:
        engine.dispose()
