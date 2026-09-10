"""Durable per-instance Gemini cooldown shared by text, audio and background work."""

import hashlib
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

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


class ProviderGate:
    def __init__(self, engine, settings, clock=None):
        self.engine = engine
        self.clock = clock or (lambda: datetime.now(UTC))
        # A changed key/model starts a new gate without persisting either credential.
        self.configuration = hashlib.sha256(
            (settings.gemini_model + "\0" + settings.gemini_api_key.get_secret_value()).encode()
        ).hexdigest()

    def call(self, request, **kwargs):
        # Dedicated connection-level lock only for provider requests. There is no
        # database transaction or global diary/ingest lock during network I/O.
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if not connection.scalar(text(f"SELECT pg_try_advisory_lock({LOCK})")):
                raise ProviderCooldown("busy", 1)
            try:
                with transaction(self.engine) as session:
                    state = session.get(AppState, KEY)
                    value = state.value if state else {}
                    if value.get("configuration") == self.configuration and value.get(
                        "blocked_until"
                    ):
                        try:
                            remaining = (
                                datetime.fromisoformat(value["blocked_until"]) - self.clock()
                            ).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            raise ProviderCooldown("invalid_state", 60) from None
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
                    self.record(reason, self.clock() + timedelta(seconds=seconds))
                    raise
                self.record("ready", None)
                return result
            finally:
                connection.execute(text(f"SELECT pg_advisory_unlock({LOCK})"))

    def record(self, reason, deadline):
        with transaction(self.engine) as session:
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
