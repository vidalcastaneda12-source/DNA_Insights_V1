"""Test fixtures shared across the suite."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# Predictable env defaults so the tests can run on any machine.
_TEST_PASSPHRASE = "test-passphrase-not-a-real-secret"


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Point Settings at a tmp directory and reset the cached singleton."""
    duckdb_path = tmp_path / "genome.duckdb"
    app_db_path = tmp_path / "app.db"
    archive_path = tmp_path / "archive"

    env = {
        "GENOME_DUCKDB_PATH": str(duckdb_path),
        "APP_DB_PATH": str(app_db_path),
        "APP_DB_PASSPHRASE": _TEST_PASSPHRASE,
        "ARCHIVE_PATH": str(archive_path),
        "EXTERNAL_CALLS_ENABLED": "false",
        "LLM_MODEL": "claude-opus-4-7",
        "LOG_LEVEL": "WARNING",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    # Force pydantic-settings to skip any local .env so tests stay hermetic.
    monkeypatch.chdir(tmp_path)

    from genome.config import get_settings  # noqa: PLC0415 — import after env is set

    get_settings.cache_clear()
    try:
        yield env
    finally:
        get_settings.cache_clear()
        # Drop env vars we set so subsequent tests start clean.
        for key in env:
            os.environ.pop(key, None)
