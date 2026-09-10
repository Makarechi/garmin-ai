from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from garmin_ai.caffeine_presets import CaffeinePreset


class ApiToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: SecretStr
    scopes: set[Literal["read:health", "read:diary", "write:diary", "admin"]] = Field(
        default_factory=lambda: {"read:health"}
    )

    @field_validator("key")
    @classmethod
    def strong_key(cls, value):
        if len(value.get_secret_value()) < 32 or value.get_secret_value().startswith(
            "replace-with-"
        ):
            raise ValueError("API token must contain at least 32 non-placeholder characters")
        return value


class ProviderConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["gemini"]
    model: str = Field(min_length=1, max_length=200)
    categories: set[Literal["health", "diary", "audio"]] = Field(min_length=1)
    granted_at: AwareDatetime
    policy_revision: Literal[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GA_", env_file=".env", extra="ignore")
    timezone: str = "Europe/Bratislava"
    token_dir: Path = Path("tokens/garmin")
    data_dir: Path = Path("data")
    database_url: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")
    api_tokens: list[ApiToken] = Field(default_factory=list, max_length=32)
    mcp_enable_writes: bool = False
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_user_id: int = 0
    telegram_webhook_secret: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = ""
    gemini_thinking_level: str = ""
    llm_enabled: bool = False
    llm_consent: ProviderConsent | None = None
    proactive_enabled: bool = False
    caffeine_presets: list[CaffeinePreset] = Field(default_factory=list, max_length=12)
    question_budget: int = 2
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    backup_key: SecretStr = SecretStr("")
    backup_dir: Path = Path("backups")
    lock_dir: Path = Path(".state")
    backup_keep_daily: int = Field(default=14, ge=1, le=365)
    backfill_days: int = Field(default=365, ge=0, le=3660)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def independent_backups(self):
        identities = [preset.id for preset in self.caffeine_presets]
        if len(identities) != len(set(identities)):
            raise ValueError("Caffeine preset identities must be distinct")
        keys = [token.key.get_secret_value() for token in self.api_tokens]
        legacy = self.api_key.get_secret_value()
        if legacy:
            keys.append(legacy)
        if len(keys) != len(set(keys)):
            raise ValueError("API credentials must be distinct")
        if "lock_dir" not in self.model_fields_set:
            anchor = next((p for p in (self.data_dir, self.token_dir) if p.is_absolute()), None)
            self.lock_dir = (anchor.parent / ".state") if anchor else self.lock_dir.resolve()
        elif not self.lock_dir.is_absolute():
            if self.data_dir.is_absolute() or self.token_dir.is_absolute():
                raise ValueError("GA_LOCK_DIR must be absolute with absolute storage directories")
            self.lock_dir = self.lock_dir.resolve()
        data, tokens = self.data_dir.resolve(), self.token_dir.resolve()
        if data.is_relative_to(tokens) or tokens.is_relative_to(data):
            raise ValueError("Data and token directories must not overlap")
        if any(
            self.lock_dir.resolve().is_relative_to(path.resolve())
            for path in (self.data_dir, self.token_dir)
        ):
            raise ValueError("Lock directory must be outside data and token directories")
        if self.backup_dir.resolve().is_relative_to(
            self.data_dir.resolve()
        ) or self.backup_dir.resolve().is_relative_to(self.token_dir.resolve()):
            raise ValueError("GA_BACKUP_DIR must be outside private source directories")
        return self
