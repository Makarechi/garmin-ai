"""Lazy integration registry; importing core never imports optional SDKs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec
from typing import Any, Literal

from pydantic import Field

from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.events import StrictModel

IntegrationKind = Literal["source", "channel", "model"]
Factory = Callable[[Settings, str], Any]
ConfigurationCheck = Callable[[Settings], str | None]


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


@dataclass(frozen=True)
class IntegrationFactory:
    kind: IntegrationKind
    provider: str
    factory: Factory
    required_modules: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    configuration_check: ConfigurationCheck | None = None

    def status(self, instance_id: str, settings: Settings | None = None) -> CapabilityStatus:
        missing = [name for name in self.required_modules if not module_available(name)]
        reason = (
            "missing optional package: " + ", ".join(missing)
            if missing
            else self.configuration_check(settings)
            if settings is not None and self.configuration_check is not None
            else None
        )
        return CapabilityStatus(
            instance_id=instance_id,
            kind=self.kind,
            provider=self.provider,
            available=reason is None,
            capabilities=self.capabilities if reason is None else frozenset(),
            reason=reason,
        )


def module_available(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


class IntegrationRegistry:
    def __init__(self) -> None:
        self._factories: dict[tuple[IntegrationKind, str], IntegrationFactory] = {}

    def register(self, descriptor: IntegrationFactory) -> None:
        key = descriptor.kind, descriptor.provider
        if key in self._factories:
            raise ValueError(
                f"Integration already registered: {descriptor.kind}/{descriptor.provider}"
            )
        self._factories[key] = descriptor

    def descriptor(self, kind: IntegrationKind, provider: str) -> IntegrationFactory:
        try:
            return self._factories[(kind, provider)]
        except KeyError as exc:
            raise IntegrationUnavailable(
                f"{kind}:{provider}", "integration provider is not registered"
            ) from exc

    def status(
        self, instance: IntegrationInstance, settings: Settings | None = None
    ) -> CapabilityStatus:
        if not instance.enabled:
            return CapabilityStatus(
                instance_id=instance.id,
                kind=instance.kind,
                provider=instance.provider,
                available=False,
                reason="integration is disabled",
            )
        return self.descriptor(instance.kind, instance.provider).status(instance.id, settings)

    def create(self, instance: IntegrationInstance, settings: Settings):
        status = self.status(instance, settings)
        if not status.available:
            raise IntegrationUnavailable(instance.id, status.reason or "integration unavailable")
        return self.descriptor(instance.kind, instance.provider).factory(settings, instance.id)


def configured_instances(settings: Settings) -> list[IntegrationInstance]:
    """Return explicit instances or compatible stable IDs for legacy settings."""

    if settings.integrations:
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


def integration_statuses(
    settings: Settings, registry: IntegrationRegistry | None = None
) -> list[CapabilityStatus]:
    registry = registry or default_registry()
    statuses = []
    for instance in configured_instances(settings):
        try:
            statuses.append(registry.status(instance, settings))
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
        else "Garmin token file is not configured"
    )


def _telegram_configuration(settings: Settings) -> str | None:
    if not settings.telegram_bot_token.get_secret_value():
        return "Telegram token is not configured"
    if not settings.telegram_user_id:
        return "Telegram owner is not configured"
    return None


def _gemini_configuration(settings: Settings) -> str | None:
    if not settings.llm_enabled:
        return "model integration is disabled by policy"
    if not settings.gemini_api_key.get_secret_value():
        return "Gemini API key is not configured"
    if not settings.gemini_model:
        return "Gemini model is not configured"
    return None


def default_registry() -> IntegrationRegistry:
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
    return registry
