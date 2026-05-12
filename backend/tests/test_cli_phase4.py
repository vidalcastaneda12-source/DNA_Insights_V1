"""CLI smoke tests for the Phase 4 surface.

Verifies that ``genome config`` and ``genome imputation`` are wired correctly
into the Typer app and that their ``--help`` output exists. Functional
behavior is covered by the module-specific test files; this file only
asserts the CLI plumbing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.cli import app
from genome.db import init_databases
from genome.db.sqlite_conn import sqlcipher_connection

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Restore structlog defaults after each test.

    The CLI's ``_configure_logging`` mutates structlog's global state. Without
    this fixture a subsequent test (e.g. ``test_ingest_liftover``) sees a
    configured logger and the WARNING level filter swallows the INFO message
    those tests assert on.
    """
    try:
        yield
    finally:
        structlog.reset_defaults()


def test_config_get_returns_seeded_value(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["config", "get", "external_calls_enabled"])
    assert result.exit_code == 0
    # Seed default is 'true' (per init_schema.py USER_PREFERENCES_SEED).
    assert "true" in result.output
    assert "value_type=boolean" in result.output


def test_config_get_handles_missing_key(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["config", "get", "nope_not_there"])
    assert result.exit_code == 0
    assert "<not set>" in result.output


def test_config_set_updates_existing_key_and_writes_audit_row(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["config", "set", "external_calls_enabled", "false"],
    )
    assert result.exit_code == 0
    # The new value is reflected in the DB.
    with sqlcipher_connection() as conn:
        value = conn.execute(
            "SELECT pref_value FROM user_preferences WHERE pref_key=?",
            ("external_calls_enabled",),
        ).fetchone()[0]
        audit_rows = conn.execute(
            "SELECT action_type, resource_id, operation_details "
            "FROM audit_log WHERE action_type='config_change'",
        ).fetchall()
    assert value == "false"
    assert len(audit_rows) == 1
    assert audit_rows[0][1] == "external_calls_enabled"
    # Operation details JSON should reflect old → new transition.
    assert "true" in audit_rows[0][2]
    assert "false" in audit_rows[0][2]


def test_config_set_requires_value_type_for_new_key(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["config", "set", "brand_new_key", "x"])
    assert result.exit_code != 0
    assert "value-type" in result.output.lower() or "value-type" in str(result.exception).lower()


def test_config_set_creates_new_key_with_value_type(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["config", "set", "new_key", "hello", "--value-type", "string"],
    )
    assert result.exit_code == 0
    with sqlcipher_connection() as conn:
        row = conn.execute(
            "SELECT pref_value, value_type FROM user_preferences WHERE pref_key='new_key'",
        ).fetchone()
    assert row == ("hello", "string")


def test_config_set_rejects_invalid_value_type(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["config", "set", "another_key", "x", "--value-type", "bogus"],
    )
    assert result.exit_code != 0


def test_imputation_help_top_level_and_each_subcommand(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    runner = CliRunner()
    top = runner.invoke(app, ["imputation", "--help"])
    assert top.exit_code == 0
    for cmd in ("prepare", "status", "download", "import", "list"):
        # Each command should appear in the parent help.
        assert cmd in top.output, f"{cmd!r} missing from `imputation --help`"
        sub = runner.invoke(app, ["imputation", cmd, "--help"])
        assert sub.exit_code == 0, f"{cmd} --help failed: {sub.output}"


def test_imputation_list_when_empty(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "list"])
    assert result.exit_code == 0
    assert "no imputation runs yet" in result.output
