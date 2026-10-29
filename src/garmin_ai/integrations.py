"""Lazy integration registry; importing core never imports optional SDKs."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from importlib.metadata import entry_points
from importlib.util import find_spec
from queue import Empty, Queue
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.events import StrictModel

IntegrationKind = Literal["source", "channel", "model"]
Factory = Callable[[Settings, str], Any]
ConfigurationCheck = Callable[[Settings], str | None]
ENTRY_POINT_GROUP = "garmin_ai.integrations"
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class PluginContext:
    """Only the named instance's validated values reach an external factory."""

    instance_id: str
    config: BaseModel
    secrets: MappingProxyType


class EmptyPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntegrationUnavailable(RuntimeError):
    def __init__(self, instance_id: str, reason: str):
        super().__init__(reason)
        self.instance_id = instance_id
        self.reason = reason


class CapabilityStatus(StrictModel):
    instance_id: str
    kind: IntegrationKind
    provider: str
    available: bool
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    reason: str | None = None
    contract_version: int | None = None
    implementation_version: str | None = None
    verification_level: Literal["declared", "local_configuration", "unavailable"] = "unavailable"


@dataclass(frozen=True)
class IntegrationFactory:
    kind: IntegrationKind
    provider: str
    factory: Factory | None = None
    required_modules: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    configuration_check: ConfigurationCheck | None = None
    plugin_factory: Callable[[PluginContext], Any] | None = None
    config_model: type[BaseModel] = EmptyPluginConfig
    contract_version: int = CONTRACT_VERSION
    implementation_version: str = "unversioned"
    required_secret_names: frozenset[str] = frozenset()

    def status(
        self,
        instance_id: str,
        settings: Settings | None = None,
        *,
        validate_runtime: bool = True,
    ) -> CapabilityStatus:
        missing = (
            [name for name in self.required_modules if not module_available(name)]
            if validate_runtime
            else []
        )
        if missing:
            reason = "missing optional package: " + ", ".join(missing)
        elif validate_runtime and self.configuration_check is not None:
            reason = (
                self.configuration_check(settings)
                if settings is not None
                else "settings required to verify integration configuration"
            )
        else:
            reason = None
        return CapabilityStatus(
            instance_id=instance_id,
            kind=self.kind,
            provider=self.provider,
            available=reason is None,
            capabilities=self.capabilities if reason is None else frozenset(),
            reason=reason,
            contract_version=self.contract_version,
            implementation_version=self.implementation_version,
            verification_level=(
                "unavailable"
                if reason is not None
                else "local_configuration"
                if validate_runtime
                else "declared"
            ),
        )


def module_available(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


class IntegrationRegistry:
    def __init__(self) -> None:
        self._factories: dict[tuple[IntegrationKind, str], IntegrationFactory] = {}
        self._errors: dict[tuple[IntegrationKind, str], str] = {}

    def register(self, descriptor: IntegrationFactory) -> None:
        key = descriptor.kind, descriptor.provider
        if key in self._factories:
            raise ValueError(
                f"Integration already registered: {descriptor.kind}/{descriptor.provider}"
            )
        self._factories[key] = descriptor

    def descriptor(self, kind: IntegrationKind, provider: str) -> IntegrationFactory:
        if (kind, provider) in self._errors:
            raise IntegrationUnavailable(f"{kind}:{provider}", self._errors[(kind, provider)])
        try:
            return self._factories[(kind, provider)]
        except KeyError as exc:
            raise IntegrationUnavailable(
                f"{kind}:{provider}", "integration provider is not registered"
            ) from exc

    def status(
        self,
        instance: IntegrationInstance,
        settings: Settings | None = None,
        *,
        validate_runtime: bool = True,
    ) -> CapabilityStatus:
        if not instance.enabled:
            return CapabilityStatus(
                instance_id=instance.id,
                kind=instance.kind,
                provider=instance.provider,
                available=False,
                reason="integration is disabled",
            )
        descriptor = self.descriptor(instance.kind, instance.provider)
        if descriptor.plugin_factory is not None:

            def unavailable(reason: str) -> CapabilityStatus:
                return CapabilityStatus(
                    instance_id=instance.id,
                    kind=instance.kind,
                    provider=instance.provider,
                    available=False,
                    reason=reason,
                    contract_version=descriptor.contract_version,
                    implementation_version=descriptor.implementation_version,
                )

            if instance.kind == "model" and "structured_output" not in descriptor.capabilities:
                return unavailable("model plugin lacks structured_output capability")
            if validate_runtime:
                if descriptor.required_secret_names and set(instance.secret_refs) != set(
                    descriptor.required_secret_names
                ):
                    return unavailable("invalid integration secret references")
                try:
                    descriptor.config_model.model_validate(instance.config)
                except Exception:
                    return unavailable("invalid integration configuration")
                missing = [
                    name for name, ref in instance.secret_refs.items() if not os.environ.get(ref)
                ]
                if missing:
                    return unavailable("missing secret reference: " + ", ".join(missing))
        return descriptor.status(
            instance.id,
            None if descriptor.plugin_factory is not None else settings,
            validate_runtime=validate_runtime,
        )

    def create(self, instance: IntegrationInstance, settings: Settings):
        status = self.status(instance, settings)
        if not status.available:
            raise IntegrationUnavailable(instance.id, status.reason or "integration unavailable")
        descriptor = self.descriptor(instance.kind, instance.provider)
        if descriptor.plugin_factory is None:
            if descriptor.factory is None:
                raise IntegrationUnavailable(instance.id, "integration has no factory")
            return descriptor.factory(settings, instance.id)
        if not any(item == instance and item.enabled for item in configured_instances(settings)):
            raise IntegrationUnavailable(
                instance.id, "integration instance is not explicitly enabled"
            )
        if descriptor.contract_version != CONTRACT_VERSION:
            raise IntegrationUnavailable(instance.id, "incompatible integration contract")
        config = descriptor.config_model.model_validate(instance.config)
        secrets = {}
        for name, reference in instance.secret_refs.items():
            value = os.environ.get(reference)
            if not value:
                raise IntegrationUnavailable(instance.id, f"missing secret reference: {name}")
            secrets[name] = SecretStr(value)
        try:
            return descriptor.plugin_factory(
                PluginContext(instance.id, config, MappingProxyType(secrets))
            )
        except Exception as exc:
            raise IntegrationUnavailable(instance.id, "integration factory failed") from exc


def discover_configured_plugins(registry: IntegrationRegistry, settings: Settings) -> None:
    """Load only explicitly enabled entry points named kind.provider."""

    selected = {
        f"{instance.kind}.{instance.provider}"
        for instance in configured_instances(settings)
        if instance.enabled
    }
    if not selected:
        return
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        if entry.name not in selected:
            continue
        kind, _, provider = entry.name.partition(".")
        try:
            descriptor = entry.load()
            if (
                not isinstance(descriptor, IntegrationFactory)
                or descriptor.kind != kind
                or descriptor.provider != provider
                or descriptor.plugin_factory is None
                or descriptor.contract_version != CONTRACT_VERSION
            ):
                raise ValueError("Invalid integration descriptor")
            registry.register(descriptor)
        except Exception:
            registry._errors[(kind, provider)] = "integration plugin failed to load"


def integrations_explicit(settings: Settings) -> bool:
    """Whether the integration allowlist was supplied, including an empty list."""

    return "integrations" in settings.model_fields_set


def configured_instances(settings: Settings) -> list[IntegrationInstance]:
    """Return explicit instances or compatible stable IDs for legacy settings."""

    if integrations_explicit(settings):
        return list(settings.integrations)
    instances = []
    if (settings.token_dir / "garmin_tokens.json").is_file():
        instances.append(
            IntegrationInstance(id="source:garmin:primary", kind="source", provider="garmin")
        )
    if settings.telegram_bot_token.get_secret_value() and settings.telegram_user_id:
        instances.append(
            IntegrationInstance(id="channel:telegram:primary", kind="channel", provider="telegram")
        )
    if settings.llm_enabled and settings.gemini_api_key.get_secret_value():
        instances.append(
            IntegrationInstance(id="model:gemini:primary", kind="model", provider="gemini")
        )
    return instances


def configured_instance(
    settings: Settings, kind: IntegrationKind, provider: str
) -> IntegrationInstance | None:
    """Resolve one active instance without silently enabling an omitted integration."""

    return next(
        (
            item
            for item in configured_instances(settings)
            if item.enabled and item.kind == kind and item.provider == provider
        ),
        None,
    )


def configured_model_instance(settings: Settings) -> IntegrationInstance | None:
    models = [
        item for item in configured_instances(settings) if item.enabled and item.kind == "model"
    ]
    if len(models) > 1:
        raise ValueError("Configure only one enabled model instance for this runtime")
    return models[0] if models else None


class ConsentGuardedModel:
    """Recheck owner consent before each plugin model request."""

    def __init__(
        self,
        provider: Any,
        instance: IntegrationInstance,
        settings: Settings,
        capabilities: frozenset[str],
        engine=None,
    ):
        self.provider = provider
        self.instance_id = instance.id
        self.instance = instance.model_copy(deep=True)
        self.settings = settings
        self.capabilities = capabilities
        self.engine = engine
        self._inflight = Lock()
        from garmin_ai.provider_gate import gate_identity

        self._credential_generation = gate_identity(settings, self.instance)[2]
        if engine is not None:
            from garmin_ai.provider_gate import ProviderGate

            self.request_gate = ProviderGate(engine, settings, instance=instance)
        else:
            self.request_gate = None

    def _authorize(self, categories: set[str]) -> None:
        from garmin_ai.llm import ProviderConsentRequired
        from garmin_ai.provider_gate import gate_identity

        if (
            configured_model_instance(self.settings) != self.instance
            or gate_identity(self.settings, self.instance)[2] != self._credential_generation
        ):
            raise ProviderConsentRequired("Model configuration changed; recreate the client")
        _authorize_plugin_model(self.instance, self.settings, categories)
        if self.engine is not None:
            from garmin_ai.db import transaction
            from garmin_ai.provider_gate import require_onboarding_categories

            with transaction(self.engine) as session:
                require_onboarding_categories(session, categories)

    def structured(self, instruction, prompt, schema):
        return self.structured_for_categories(instruction, prompt, schema, {"health", "diary"})

    def structured_for_categories(self, instruction, prompt, schema, categories):
        from garmin_ai.llm import ProviderCapabilityUnsupported

        if "structured_output" not in self.capabilities:
            raise ProviderCapabilityUnsupported("This model does not support structured output")
        if not categories or not categories <= {"health", "diary"}:
            raise ValueError("Structured model categories must be health and/or diary")
        self._authorize(categories)

        def request():
            from garmin_ai.llm import (
                ModelResult,
                ProviderOutputInvalid,
                ProviderUnavailable,
                normalize_plugin_error,
            )

            failure = None
            try:
                response = self.provider.structured(instruction, prompt, schema)
            except Exception as exc:
                try:
                    failure = normalize_plugin_error(exc)
                except Exception:
                    failure = ProviderUnavailable("Model request failed")
            if failure is not None:
                raise failure from None
            invalid = False
            try:
                value, usage = self._unpack_result(response)
                payload = (
                    value.model_dump(mode="python", warnings=False)
                    if isinstance(value, BaseModel)
                    else value
                )
                validated = schema.model_validate(payload)
            except Exception:
                invalid = True
            if invalid:
                raise ProviderOutputInvalid("Model output failed validation") from None
            return ModelResult(value=validated, usage=usage)

        return self._request(request, "structured", categories)

    def transcribe(self, data, mime_type):
        from garmin_ai.llm import ProviderCapabilityUnsupported

        if "transcription" not in self.capabilities:
            raise ProviderCapabilityUnsupported("This model does not support transcription")
        self._authorize({"audio"})
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("Voice message exceeds 20 MB")

        def request():
            from garmin_ai.llm import (
                ModelResult,
                ProviderOutputInvalid,
                ProviderUnavailable,
                normalize_plugin_error,
            )

            failure = None
            try:
                response = self.provider.transcribe(data, mime_type)
            except Exception as exc:
                try:
                    failure = normalize_plugin_error(exc)
                except Exception:
                    failure = ProviderUnavailable("Model request failed")
            if failure is not None:
                raise failure from None
            value, usage = self._unpack_result(response)
            if not isinstance(value, str):
                raise ProviderOutputInvalid("Model transcription failed validation")
            return ModelResult(value=value, usage=usage)

        return self._request(request, "transcribe", {"audio"})

    def _request(self, request, operation, categories):
        from garmin_ai.llm import ProviderCooldown, ProviderFallbackDeadline

        started = time.monotonic()
        outcome = "error"
        usage = None
        results = Queue(maxsize=1)
        context = copy_context()
        abandoned = Event()

        def dispatch():
            if abandoned.is_set():
                raise ProviderFallbackDeadline("Model request deadline exceeded")
            self._authorize(categories)
            return request()

        def invoke():
            try:
                if not self._inflight.acquire(blocking=False):
                    raise ProviderCooldown("busy", 1)
                try:
                    result = (
                        dispatch()
                        if self.request_gate is None
                        else self.request_gate.call(dispatch, model_categories=categories)
                    )
                finally:
                    self._inflight.release()
                results.put((True, result))
            except BaseException as exc:
                results.put((False, exc))

        Thread(target=context.run, args=(invoke,), daemon=True, name="model-plugin-request").start()
        timeout = (
            self.instance.request_timeout_seconds or self.settings.model_request_timeout_seconds
        )
        try:
            try:
                succeeded, result = results.get(timeout=timeout)
            except Empty:
                abandoned.set()
                self.cancel()
                raise ProviderFallbackDeadline("Model request deadline exceeded") from None
            if not succeeded:
                raise result
            outcome = "success"
            usage = result.usage
            return result.value
        finally:
            fields = {
                "provider_instance_id": self.instance_id,
                "operation": operation,
                "outcome": outcome,
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
            if usage is not None:
                fields.update(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
            try:
                logging.getLogger("garmin_ai").info("model_plugin_request", extra=fields)
            except Exception:
                pass

    @staticmethod
    def _unpack_result(response):
        from garmin_ai.llm import ModelResult, ModelUsage, ProviderOutputInvalid

        if not isinstance(response, ModelResult):
            return response, None
        invalid = False
        try:
            raw_usage = response.usage
            if isinstance(raw_usage, BaseModel):
                raw_usage = raw_usage.model_dump(mode="python", warnings=False)
            usage = ModelUsage.model_validate(raw_usage) if raw_usage is not None else None
        except Exception:
            invalid = True
        if invalid:
            raise ProviderOutputInvalid("Model usage failed validation") from None
        return response.value, usage

    def close(self):
        self._lifecycle("close")

    def cancel(self):
        if "cancellation" not in self.capabilities:
            return False
        return self._lifecycle("cancel")

    def _lifecycle(self, operation):
        action = getattr(self.provider, operation, None)
        if not callable(action):
            return False
        complete = Event()
        success = [False]

        def invoke():
            try:
                action()
                success[0] = True
            except Exception as exc:
                logging.getLogger("garmin_ai").warning(
                    f"model_plugin_{operation}_failed", extra={"error_type": type(exc).__name__}
                )
            finally:
                complete.set()

        Thread(target=invoke, daemon=True, name="model-plugin-lifecycle").start()
        timeout = (
            self.instance.request_timeout_seconds or self.settings.model_request_timeout_seconds
        )
        complete.wait(min(timeout, 1.0))
        return complete.is_set() and success[0]


def _authorize_plugin_model(
    instance: IntegrationInstance, settings: Settings, categories: set[str]
) -> None:
    from garmin_ai.llm import ProviderConsentRequired

    consent = settings.llm_consent
    model = instance.config.get("model")
    if (
        not settings.llm_enabled
        or consent is None
        or consent.provider != instance.provider
        or consent.provider_instance_id != instance.id
        or consent.model != model
        or consent.granted_at > datetime.now(UTC)
        or not categories <= consent.categories
    ):
        raise ProviderConsentRequired("Model consent is missing or does not cover this request")


def create_model_provider(settings: Settings, engine=None):
    """Resolve the one selected model through the same registry as status reporting."""

    instance = configured_model_instance(settings)
    if instance is None:
        return None
    if instance.provider != "gemini":
        _authorize_plugin_model(instance, settings, set())
    registry = default_registry(settings)
    provider = registry.create(instance, settings)
    descriptor = registry.descriptor("model", instance.provider)
    if descriptor.plugin_factory is not None:
        guarded = ConsentGuardedModel(provider, instance, settings, descriptor.capabilities, engine)
        try:
            guarded._authorize(set())
        except BaseException:
            guarded.close()
            raise
        return guarded
    if engine is not None:
        from garmin_ai.provider_gate import ProviderGate

        provider.request_gate = ProviderGate(engine, settings, instance=instance)
    return provider


def channel_instance_id(instance: IntegrationInstance | None) -> str:
    """Return the stable transport namespace for a configured channel instance."""

    if instance is None:
        return "primary"
    prefix = f"{instance.kind}:{instance.provider}:"
    return instance.id.removeprefix(prefix) if instance.id.startswith(prefix) else instance.id


def configured_telegram_ingress_instance(settings: Settings) -> IntegrationInstance | None:
    """Resolve webhook identity without requiring outbound bot credentials."""

    if settings.integrations:
        return configured_instance(settings, "channel", "telegram")
    if settings.telegram_user_id:
        return IntegrationInstance(
            id="channel:telegram:primary",
            kind="channel",
            provider="telegram",
        )
    return None


def onboarding_allows_instance(
    instance: IntegrationInstance, preferences: dict[str, Any] | None
) -> bool:
    """Apply a completed onboarding integration allowlist to a configured instance."""

    if preferences is None:
        return True
    if instance.kind == "source":
        selected = preferences.get("source_instance_ids")
        return isinstance(selected, list) and instance.id in selected
    if instance.kind == "channel":
        selected = preferences.get("channel")
        identity = {
            "channel": instance.provider,
            "instance_id": channel_instance_id(instance),
        }
        if selected == identity:
            return True
        fallbacks = preferences.get("fallback_channels")
        return isinstance(fallbacks, list) and identity in fallbacks
    return True


def integration_statuses(
    settings: Settings,
    registry: IntegrationRegistry | None = None,
    *,
    validate_runtime: bool = True,
) -> list[CapabilityStatus]:
    registry = registry or default_registry(settings)
    statuses = []
    for instance in configured_instances(settings):
        try:
            statuses.append(registry.status(instance, settings, validate_runtime=validate_runtime))
        except IntegrationUnavailable as exc:
            statuses.append(
                CapabilityStatus(
                    instance_id=instance.id,
                    kind=instance.kind,
                    provider=instance.provider,
                    available=False,
                    reason=exc.reason,
                )
            )
    return statuses


def require_capability(status: CapabilityStatus, capability: str) -> None:
    if not status.available:
        raise IntegrationUnavailable(status.instance_id, status.reason or "integration unavailable")
    if capability not in status.capabilities:
        raise IntegrationUnavailable(
            status.instance_id,
            f"unsupported capability: {capability}",
        )


def _garmin(settings: Settings, _instance_id: str):
    from garmin_ai.garmin import GarminReader

    return GarminReader.restore(settings.token_dir)


def _telegram(settings: Settings, _instance_id: str):
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise IntegrationUnavailable(_instance_id, "Telegram token is not configured")
    Bot = import_module("telegram").Bot
    return Bot(token)


def _gemini(settings: Settings, instance_id: str):
    from garmin_ai.llm import GeminiProvider

    return GeminiProvider(settings, instance_id=instance_id)


def _garmin_configuration(settings: Settings) -> str | None:
    return (
        None
        if (settings.token_dir / "garmin_tokens.json").is_file()
        else "Garmin tokens are not configured"
    )


def _telegram_configuration(settings: Settings) -> str | None:
    if not settings.telegram_bot_token.get_secret_value() or not settings.telegram_user_id:
        return "Telegram token and owner are not configured"
    return None


def _gemini_configuration(settings: Settings) -> str | None:
    if (
        not settings.llm_enabled
        or not settings.gemini_api_key.get_secret_value()
        or not settings.gemini_model
    ):
        return "Gemini model credentials are not configured"
    return None


def default_registry(settings: Settings | None = None) -> IntegrationRegistry:
    from garmin_ai.openai_adapter import OpenAIConfig, create_openai_provider

    registry = IntegrationRegistry()
    registry.register(
        IntegrationFactory(
            kind="source",
            provider="garmin",
            factory=_garmin,
            required_modules=("garminconnect",),
            configuration_check=_garmin_configuration,
            capabilities=frozenset({"health_metrics", "activities", "fit_files"}),
        )
    )
    registry.register(
        IntegrationFactory(
            kind="channel",
            provider="telegram",
            factory=_telegram,
            required_modules=("telegram",),
            configuration_check=_telegram_configuration,
            capabilities=frozenset({"text", "actions", "initiatives"}),
        )
    )
    registry.register(
        IntegrationFactory(
            kind="model",
            provider="gemini",
            factory=_gemini,
            required_modules=("google.genai",),
            configuration_check=_gemini_configuration,
            capabilities=frozenset({"structured_output", "transcription"}),
        )
    )
    registry.register(
        IntegrationFactory(
            kind="model",
            provider="openai",
            plugin_factory=create_openai_provider,
            config_model=OpenAIConfig,
            capabilities=frozenset({"structured_output"}),
            implementation_version="openai-responses-v1",
            required_secret_names=frozenset({"api_key"}),
        )
    )
    if settings is not None:
        discover_configured_plugins(registry, settings)
    return registry
