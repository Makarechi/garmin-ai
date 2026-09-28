"""Durable per-instance Gemini cooldown shared by text, audio and background work."""

import hashlib
import json
import math
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from garmin_ai.db import transaction
from garmin_ai.llm import (
    ProviderAuthError,
    ProviderConsentRequired,
    ProviderCooldown,
    ProviderFallbackDeadline,
    ProviderModelUnavailable,
    ProviderOutputInvalid,
    ProviderRateLimited,
    ProviderRequestInvalid,
    ProviderUnavailable,
)
from garmin_ai.models import AppState, Job
from garmin_ai.normalize import upsert

KEY = "provider:gemini:gate"
LOCK = 72104634
ACTIVE_JOB = ContextVar("provider_gate_active_job", default=None)
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
                available_models = list(models) if models is not None else [None]
                request_fingerprint = hashlib.sha256(
                    repr((available_models, sorted(model_categories), kwargs)).encode()
                ).hexdigest()
                operation_id = ACTIVE_JOB.get()
                resume_models = set()
                with transaction(self.engine) as session:
                    require_onboarding_categories(session, model_categories)
                    if operation_id is not None:
                        job = session.get(Job, operation_id)
                        resume = job.payload.get("provider_resume", {}) if job else {}
                        if (
                            resume.get("configuration") == self.configuration
                            and resume.get("models") == available_models
                            and resume.get("request_fingerprint") == request_fingerprint
                        ):
                            resume_models = set(resume.get("invalid_models", []))
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
                    now = self.clock()
                    pending = []
                    cooling_models = []
                    for model in available_models:
                        if model in resume_models:
                            continue
                        if model is None or model not in model_cooldowns:
                            pending.append(model)
                            continue
                        remaining = (
                            datetime.fromisoformat(model_cooldowns[model]) - now
                        ).total_seconds()
                        if remaining <= 0:
                            pending.append(model)
                        else:
                            cooling_models.append(model)
                    if not pending:
                        if resume_models.issuperset(available_models):
                            self.clear_resume(operation_id)
                            raise ProviderOutputInvalid("All Gemini models returned invalid output")
                        earliest = min(
                            datetime.fromisoformat(model_cooldowns[model])
                            for model in cooling_models
                        )
                        seconds = max(1, math.ceil((earliest - now).total_seconds()))
                        self.record_outcome("model_cooldown", earliest, model_cooldowns)
                        raise ProviderCooldown("model_cooldown", seconds)

                    failures = []
                    failed_models = {}
                    invalid_models = set()
                    deadline_error = None
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
                            if failures or cooling_models or model != available_models[0]:
                                if model is not None:
                                    model_cooldowns[model] = (
                                        self.clock() + timedelta(seconds=exc.retry_seconds)
                                    ).isoformat()
                                    failed_models[model] = exc
                                failures.append(exc)
                                self.record_outcome("ready", None, model_cooldowns)
                                continue
                            self.record_outcome(
                                "auth",
                                self.clock() + timedelta(seconds=exc.retry_seconds),
                                model_cooldowns,
                            )
                            raise
                        except ProviderFallbackDeadline as exc:
                            deadline_error = exc
                            break
                        except ProviderRequestInvalid:
                            if any(isinstance(exc, ProviderUnavailable) for exc in failures):
                                earliest = min(
                                    datetime.fromisoformat(model_cooldowns[item])
                                    for item in failed_models
                                )
                                seconds = max(
                                    1, math.ceil((earliest - self.clock()).total_seconds())
                                )
                                self.record_outcome(
                                    "ready", None, model_cooldowns, scheduler_pause_until=earliest
                                )
                                raise ProviderCooldown("model_cooldown", seconds) from None
                            if cooling_models:
                                earliest = min(
                                    datetime.fromisoformat(model_cooldowns[item])
                                    for item in cooling_models
                                )
                                seconds = max(
                                    1, math.ceil((earliest - self.clock()).total_seconds())
                                )
                                self.record_outcome(
                                    "ready", None, model_cooldowns, scheduler_pause_until=earliest
                                )
                                raise ProviderCooldown("model_cooldown", seconds) from None
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
                                failed_models[model] = exc
                            failures.append(exc)
                            self.record_outcome("ready", None, model_cooldowns)
                        except ProviderOutputInvalid as exc:
                            failures.append(exc)
                            invalid_models.add(model)
                        else:
                            self.clear_resume(operation_id)
                            self.record_outcome("ready", None, model_cooldowns)
                            return result

                    if deadline_error is not None:
                        if (
                            invalid_models
                            and operation_id is not None
                            and self.record_resume(
                                operation_id,
                                available_models,
                                request_fingerprint,
                                resume_models | invalid_models,
                            )
                        ):
                            raise ProviderCooldown("model_cooldown", 1) from deadline_error
                        raise deadline_error

                    if failures:
                        if invalid_models and (resume_models | invalid_models).issuperset(
                            available_models
                        ):
                            self.clear_resume(operation_id)
                            raise next(
                                exc for exc in failures if isinstance(exc, ProviderOutputInvalid)
                            )
                        if invalid_models and not cooling_models:
                            raise next(
                                exc for exc in failures if isinstance(exc, ProviderOutputInvalid)
                            )
                        if models is not None and len(available_models) > 1:
                            pending_deadlines = [
                                datetime.fromisoformat(model_cooldowns[model])
                                for model in available_models
                                if model in model_cooldowns
                            ]
                            if pending_deadlines:
                                earliest = min(pending_deadlines)
                                seconds = max(
                                    1, math.ceil((earliest - self.clock()).total_seconds())
                                )
                                quota = any(
                                    isinstance(exc, ProviderRateLimited) for exc in failures
                                )
                                failed_model = next(
                                    (
                                        model
                                        for model, deadline in model_cooldowns.items()
                                        if datetime.fromisoformat(deadline) == earliest
                                    ),
                                    None,
                                )
                                error = failed_models.get(failed_model)
                                every_model_cooling = all(
                                    model in model_cooldowns for model in available_models
                                )
                                if every_model_cooling:
                                    self.record_outcome(
                                        "quota" if quota else "unavailable",
                                        earliest,
                                        model_cooldowns,
                                    )
                                elif error is None:
                                    self.record_outcome("model_cooldown", earliest, model_cooldowns)
                                if error is not None:
                                    error.retry_seconds = seconds
                                    raise error
                                raise ProviderCooldown("model_cooldown", seconds)
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

    def record_resume(self, operation_id, models, request_fingerprint, invalid_models):
        try:
            with transaction(self.engine) as session:
                job = session.get(Job, operation_id, with_for_update=True)
                if job is None:
                    return False
                job.payload = {
                    **job.payload,
                    "provider_resume": {
                        "configuration": self.configuration,
                        "models": models,
                        "request_fingerprint": request_fingerprint,
                        "invalid_models": [model for model in models if model in invalid_models],
                    },
                }
            return True
        except SQLAlchemyError:
            return False

    def clear_resume(self, operation_id):
        if operation_id is None:
            return
        try:
            with transaction(self.engine) as session:
                job = session.get(Job, operation_id, with_for_update=True)
                if job is not None and "provider_resume" in job.payload:
                    job.payload = {
                        key: value for key, value in job.payload.items() if key != "provider_resume"
                    }
        except SQLAlchemyError:
            pass

    def record_outcome(self, reason, deadline, model_cooldowns=None, *, scheduler_pause_until=None):
        try:
            if scheduler_pause_until is None:
                self.record(reason, deadline, model_cooldowns)
            else:
                self.record(
                    reason, deadline, model_cooldowns, scheduler_pause_until=scheduler_pause_until
                )
        except SQLAlchemyError:
            # The response already exists; persistence failure must not repeat
            # a paid request or mask its typed retry deadline.
            pass

    def record(self, reason, deadline, model_cooldowns=None, *, scheduler_pause_until=None):
        with transaction(self.engine) as session:
            if reason == "quota" and self.notifications_enabled:
                enqueue_quota_notice(session, self.clock())
            existing = session.get(AppState, KEY)
            prior = existing.value if existing and isinstance(existing.value, dict) else {}
            if prior.get("configuration") == self.configuration:
                try:
                    previous_pause = datetime.fromisoformat(prior["scheduler_pause_until"])
                    if previous_pause > self.clock() and (
                        scheduler_pause_until is None or previous_pause > scheduler_pause_until
                    ):
                        scheduler_pause_until = previous_pause
                except (KeyError, TypeError, ValueError, OverflowError):
                    pass
            upsert(
                session,
                AppState,
                {
                    "key": KEY,
                    "value": {
                        "configuration": self.configuration,
                        "reason": reason,
                        "blocked_until": deadline.isoformat() if deadline else None,
                        "scheduler_pause_until": (
                            scheduler_pause_until.isoformat() if scheduler_pause_until else None
                        ),
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
    if (
        state is None
        or not isinstance(state.value, dict)
        or state.value.get("configuration") != configuration_key(settings)
    ):
        return False
    for key in ("blocked_until", "scheduler_pause_until"):
        try:
            if datetime.fromisoformat(state.value[key]) > now:
                return True
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return False
