"""Settings load from the environment / .env."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from genome.config import Settings, get_settings

if TYPE_CHECKING:
    import pytest


def test_settings_load_from_env(isolated_settings: dict[str, str]) -> None:
    settings = get_settings()
    assert settings.genome_duckdb_path == Path(isolated_settings["GENOME_DUCKDB_PATH"])
    assert settings.app_db_path == Path(isolated_settings["APP_DB_PATH"])
    assert settings.app_db_passphrase.get_secret_value() == isolated_settings["APP_DB_PASSPHRASE"]
    assert settings.archive_path == Path(isolated_settings["ARCHIVE_PATH"])
    assert settings.external_calls_enabled is False
    assert settings.llm_model == "claude-opus-4-7"
    assert settings.log_level == "WARNING"


def test_passphrase_is_not_logged_in_repr(isolated_settings: dict[str, str]) -> None:
    settings = get_settings()
    rendered = repr(settings)
    assert isolated_settings["APP_DB_PASSPHRASE"] not in rendered
    assert "**" in rendered or "SecretStr" in rendered


def test_settings_dotenv_fallback_to_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults fill in when only the required passphrase is set."""
    monkeypatch.chdir(tmp_path)
    for key in (
        "GENOME_DUCKDB_PATH",
        "APP_DB_PATH",
        "ARCHIVE_PATH",
        "EXTERNAL_CALLS_ENABLED",
        "LLM_MODEL",
        "LOG_LEVEL",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_DB_PASSPHRASE", "x")

    get_settings.cache_clear()
    try:
        s = Settings()  # type: ignore[call-arg]
        assert s.genome_duckdb_path == Path("data/genome.duckdb")
        assert s.app_db_path == Path("data/app.db")
        assert s.archive_path == Path("archive")
        assert s.external_calls_enabled is False
        assert s.llm_model == "claude-opus-4-7"
        assert s.log_level == "INFO"
    finally:
        get_settings.cache_clear()
