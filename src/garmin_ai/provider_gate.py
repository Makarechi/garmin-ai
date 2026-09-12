"""Durable per-instance Gemini cooldown shared by text, audio and background work."""

import hashlib
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from garmin_ai.db import transaction
from garmin_ai.llm import (
    ProviderAuthError,
    ProviderConsentRequired,
    ProviderCooldown,
    ProviderModelUnavailable,
    ProviderRateLimited,
    ProviderUnavailable,
)
from garmin_ai.models import AppState
from garmin_ai.normalize import upsert

KEY = "provider:gemini:gate"
LOCK = 72104634
QUOTA_NOTICE = "Gemini временно отклонил запрос из-за лимита API. Запросы к модели приостановлены. Команды /today, /status и формы дневника доступны."


def enqueue_quota_notice(session, now):
    from garmin_ai.jobs import enqueue

    key = f"quota:{now:%Y-%m-%d-%H}"
    enqueue(session, "telegram_provider_notice", {"outbox_key": key}, "provider-notice:" + key, now)


class ProviderGate:
    def __init__(self, engine, settings, clock=None):
        self.engine = engine
        self.notifications_enabled = bool(
            settings.telegram_user_id and settings.telegram_bot_token.get_secret_value()
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        # A changed key/model starts a new gate without persisting either credential.
        self.configuration = configuration_key(settings)

    def call(self, request, **kwargs):
        # Dedicated connection-level lock only for provider requests. There is no
        # database transaction or global diary/ingest lock during network I/O.
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if not connection.scalar(text(f"SELECT pg_try_advisory_lock({LOCK})")):
                raise ProviderCooldown("busy", 1)
            try:
                with transaction(self.engine) as session:
                    state = session.get(AppState, KEY)
                    value = state.value if state and isinstance(state.value, dict) else {}
                    if value.get("configuration") == self.configuration and value.get(
                        "blocked_until"
                    ):
                        try:
                            remaining = (
                                datetime.fromisoformat(value["blocked_until"]) - self.clock()
                            ).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            # Keep the provider lock and perform one recovery probe;
                            # success/failure below replaces the malformed state.
                            remaining = 0
                        if remaining > 0:
                            raise ProviderCooldown(
                                value.get("reason", "unavailable"), math.ceil(remaining)
                            )
                try:
                    result = request(**kwargs)
                except ProviderConsentRequired:
                    raise
                except ProviderUnavailable as exc:
                    reason = (
                        "quota"
                        if isinstance(exc, ProviderRateLimited)
                        else "auth"
                        if isinstance(exc, ProviderAuthError)
                        else "model"
                        if isinstance(exc, ProviderModelUnavailable)
                        else "unavailable"
                    )
                    seconds = getattr(exc, "retry_seconds", 60)
                    self.record_outcome(reason, self.clock() + timedelta(seconds=seconds))
                    raise
                self.record_outcome("ready", None)
                return result
            finally:
                try:
                    connection.execute(text(f"SELECT pg_advisory_unlock({LOCK})"))
                except SQLAlchemyError:
                    # A lost PostgreSQL session already released its advisory lock.
                    # Cleanup must not replace a successful response or provider error.
                    connection.invalidate()

    def record_outcome(self, reason, deadline):
        try:
            self.record(reason, deadline)
        except SQLAlchemyError:
            # The response already exists; persistence failure must not repeat
            # a paid request or mask its typed retry deadline.
            pass

    def record(self, reason, deadline):
        with transaction(self.engine) as session:
            if reason == "quota" and self.notifications_enabled:
                enqueue_quota_notice(session, self.clock())
            upsert(
                session,
                AppState,
                {
                    "key": KEY,
                    "value": {
                        "configuration": self.configuration,
                        "reason": reason,
                        "blocked_until": deadline.isoformat() if deadline else None,
                        "at": self.clock().isoformat(),
                    },
                },
                ["key"],
            )


def configuration_key(settings):
    return hashlib.sha256(
        (settings.gemini_model + "\0" + settings.gemini_api_key.get_secret_value()).encode()
    ).hexdigest()


def paused(session, now=None, *, settings=None):
    if settings is None:
        return False
    now = now or datetime.now(UTC)
    state = session.get(AppState, KEY, populate_existing=True)
    try:
        return bool(
            state
            and isinstance(state.value, dict)
            and state.value.get("configuration") == configuration_key(settings)
            and datetime.fromisoformat(state.value["blocked_until"]) > now
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
