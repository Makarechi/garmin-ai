from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GA_", env_file=".env", extra="ignore")
    timezone: str = "Europe/Bratislava"
    token_dir: Path = Path("tokens/garmin")
    data_dir: Path = Path("data")
    database_url: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_user_id: int = 0
    telegram_webhook_secret: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = ""
    gemini_thinking_level: str = "low"
    llm_enabled: bool = False
    proactive_enabled: bool = False
    question_budget: int = 2
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    backup_key: SecretStr = SecretStr("")
    backup_dir: Path = Path("backups")
    backup_keep_daily: int = Field(default=14, ge=1, le=365)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def independent_backups(self):
        if self.backup_dir.resolve().is_relative_to(
            self.data_dir.resolve()
        ) or self.backup_dir.resolve().is_relative_to(self.token_dir.resolve()):
            raise ValueError("GA_BACKUP_DIR must be outside private source directories")
        return self
