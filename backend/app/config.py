"""Runtime configuration from environment variables (env-driven, no magic)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

GovernanceMode = Literal["observe", "enforce"]
RecorderFailureMode = Literal["fail_closed", "allow"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = (
        "postgresql+asyncpg://governance:governance@localhost:5433/governance"
    )

    governance_mode: GovernanceMode = "enforce"
    governance_on_recorder_failure: RecorderFailureMode = "fail_closed"

    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str = "gpt-4o-mini"


@lru_cache
def get_settings() -> Settings:
    return Settings()
