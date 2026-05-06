"""Application configuration loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Required values raise on access if absent. Defaults match docs/.env.example.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    genome_duckdb_path: Path = Field(default=Path("data/genome.duckdb"))
    app_db_path: Path = Field(default=Path("data/app.db"))
    app_db_passphrase: SecretStr
    archive_path: Path = Field(default=Path("archive"))
    external_calls_enabled: bool = Field(default=False)
    llm_model: str = Field(default="claude-opus-4-7")
    anthropic_api_key: SecretStr | None = Field(default=None)
    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance.

    Loaded once per process; tests should call ``get_settings.cache_clear()`` between cases.
    """
    return Settings()  # type: ignore[call-arg]
