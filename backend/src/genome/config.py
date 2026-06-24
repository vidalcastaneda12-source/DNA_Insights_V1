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
    # Override the default reference panel root (~/.cache/genome/imputation/).
    # Useful for shared-storage setups where the panel lives on an external drive.
    imputation_panel_root: Path | None = Field(default=None)
    # Override the default annotations download cache root
    # (~/.cache/genome/annotations/). Sibling of imputation_panel_root.
    annotations_download_root: Path | None = Field(default=None)
    llm_model: str = Field(default="claude-opus-4-7")
    anthropic_api_key: SecretStr | None = Field(default=None)
    log_level: str = Field(default="INFO")


class LoggingSettings(BaseSettings):
    """Logging-only settings, decoupled from the DB credentials.

    Logging must configure on *every* ``genome`` invocation — including ``genome docs
    check`` on a fresh checkout with no ``.env`` — so it reads only ``log_level`` and
    never requires ``app_db_passphrase``. DB commands still build the full
    :class:`Settings` and fail loudly when the passphrase is absent.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance.

    Loaded once per process; tests should call ``get_settings.cache_clear()`` between cases.
    """
    return Settings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_logging_settings() -> LoggingSettings:
    """Return the cached logging-only settings (no DB passphrase required)."""
    return LoggingSettings()
