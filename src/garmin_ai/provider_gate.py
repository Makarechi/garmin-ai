"""Durable per-instance Gemini cooldown shared by text, audio and background work."""

import hashlib
import json
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
    ProviderOutputInvalid,
    ProviderRateLimited,
    ProviderRequestInvalid,
    ProviderUnavailable,
)
from garmin_ai.models import AppState
from garmin_ai.normalize import upsert

KEY = "provider:gemini:gate"
LOCK = 72104634
ONBOARDING_KEY = "preferences:onboarding"
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

    def call(self, request, *, model_categories=frozenset(), models=None, **kwargs):
        # Dedicated connection-level lock only for provider requests. There is no
        # database transaction or global diary/ingest lock during network I/O.
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if not connection.scalar(text(f"SELECT pg_try_advisory_lock({LOCK})")):
                raise ProviderCooldown("busy", 1)
            try:
                model_cooldowns = {}
                with transaction(self.engine) as session:
                    require_onboarding_categories(session, model_categories)
                    state = session.get(AppState, KEY)
                    value = state.value if state and isinstance(state.value, dict) else {}
                    if value.get("configuration") == self.configuration:
                        stored_cooldowns = value.get("model_cooldowns", {})
                        if isinstance(stored_cooldowns, dict):
                            now = self.clock()
                            for model, raw_deadline in stored_cooldowns.items():
                                try:
                                    deadline = datetime.fromisoformat(raw_deadline)
                                except (TypeError, ValueError, OverflowError):
                                    continue
                                if deadline.tzinfo is None or deadline.utcoffset() is None:
                                    continue
                                if deadline > now:
                                    model_cooldowns[model] = deadline.isoformat()
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
                    available_models = list(models) if models is not None else [None]
                    now = self.clock()
                    pending = []
                    for model in available_models:
                        if model is None or model not in model_cooldowns:
                            pending.append(model)
                            continue
                        remaining = (
                            datetime.fromisoformat(model_cooldowns[model]) - now
                        ).total_seconds()
                        if remaining <= 0:
                            pending.append(model)
                    if not pending:
                        earliest = min(
                            datetime.fromisoformat(model_cooldowns[model])
                            for model in available_models
                        )
                        raise ProviderCooldown(
                            "model_cooldown", max(1, math.ceil((earliest - now).total_seconds()))
                        )

                    failures = []
                    for model in pending:
                        try:
                            result = (
                                request(model=model, **kwargs)
                                if model is not None
                                else request(**kwargs)
                            )
                        except ProviderConsentRequired:
                            raise
                        except ProviderCooldown:
                            raise
                        except ProviderAuthError as exc:
                            self.record_outcome(
                                "auth",
                                self.clock() + timedelta(seconds=exc.retry_seconds),
                                model_cooldowns,
                            )
                            raise
                        except ProviderRequestInvalid:
                            if failures:
                                break
                            raise
                        except (
                            ProviderRateLimited,
                            ProviderModelUnavailable,
                            ProviderUnavailable,
                        ) as exc:
                            seconds = getattr(exc, "retry_seconds", 120)
                            if model is not None:
                                model_cooldowns[model] = (
                                    self.clock() + timedelta(seconds=seconds)
                                ).isoformat()
                            failures.append(exc)
                            self.record_outcome("ready", None, model_cooldowns)
                        except ProviderOutputInvalid as exc:
                            failures.append(exc)
                        else:
                            self.record_outcome("ready", None, model_cooldowns)
                            return result

                    if failures:
                        error = next(
                            (exc for exc in failures if isinstance(exc, ProviderRateLimited)),
                            failures[0],
                        )
                        raise error
                    raise ProviderUnavailable("Gemini has no authorized model")
                except ProviderConsentRequired:
                    raise
                except (ProviderCooldown, ProviderRequestInvalid):
                    raise
                except ProviderUnavailable as exc:
                    if isinstance(exc, ProviderAuthError):
                        raise
                    if models is not None and len(models) > 1:
                        reason = "quota" if isinstance(exc, ProviderRateLimited) else "ready"
                        self.record_outcome(reason, None, model_cooldowns)
                        raise
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
                    self.record_outcome(
                        reason, self.clock() + timedelta(seconds=seconds), model_cooldowns
                    )
                    raise
                except ProviderOutputInvalid:
                    self.record_outcome("ready", None, model_cooldowns)
                    raise
            finally:
                try:
                    connection.execute(text(f"SELECT pg_advisory_unlock({LOCK})"))
                except SQLAlchemyError:
                    # A lost PostgreSQL session already released its advisory lock.
                    # Cleanup must not replace a successful response or provider error.
                    connection.invalidate()

    def record_outcome(self, reason, deadline, model_cooldowns=None):
        try:
            self.record(reason, deadline, model_cooldowns)
        except SQLAlchemyError:
            # The response already exists; persistence failure must not repeat
            # a paid request or mask its typed retry deadline.
            pass

    def record(self, reason, deadline, model_cooldowns=None):
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
                        "model_cooldowns": model_cooldowns or {},
                        "at": self.clock().isoformat(),
                    },
                },
                ["key"],
            )


def require_onboarding_categories(session, categories) -> None:
    """Keep the persisted onboarding choices as a separate provider boundary."""

    saved = session.get(AppState, ONBOARDING_KEY)
    if saved is None:
        return
    value = saved.value if isinstance(saved.value, dict) else {}
    allowed = value.get("model_categories", [])
    if not isinstance(allowed, list) or not set(categories) <= set(allowed):
        raise ProviderConsentRequired("Onboarding model choices do not cover this request")


def configuration_key(settings):
    fallback_models = (
        settings.llm_consent.fallback_models if settings.llm_consent is not None else []
    )
    return hashlib.sha256(
        json.dumps(
            [
                settings.gemini_model,
                settings.gemini_fallback_enabled,
                settings.gemini_fallback_models,
                fallback_models,
                settings.gemini_api_key.get_secret_value(),
            ],
            separators=(",", ":"),
        ).encode()
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
