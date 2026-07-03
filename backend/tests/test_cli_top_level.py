"""Tests-only lock module for the top-level ``genome`` CLI commands.

Locks the previously-uncovered top-level CLI surface — ``genome init``,
``genome status``, and ``genome version`` — by asserting the current behavior of
``cli.py``. It adds no production code; every test passes against the CLI as it
stands (audit item 3.2, RM-c5bcb2d / PR 12).

``genome config get|set`` is intentionally NOT covered here — it is already locked
in ``test_cli_phase4.py`` (the Phase 4 config surface). This module deliberately
does not re-test it, to avoid duplicate coverage.

The suite as a whole exercises three *distinct* ``External calls enabled:`` states,
so no reviewer should read them as duplicated coverage:

* ``test_cli_phase4.py`` — the finding-024 *under-report* direction: env ``false``
  but ``config set external_calls_enabled true`` writes ``user_preferences``, so
  ``status`` must report ``True`` (the pre-fix code read the ``.env`` snapshot and
  wrongly showed ``False``).
* here, ``test_status_after_init_reports_counts_and_seed_false_external`` — the
  seed-default state: a fresh ``init`` with no ``config set`` reports ``False``.
* here, ``test_status_reports_false_when_env_true_but_pref_false`` — the finding-024
  *over-report* discriminator: env ``true`` but the seeded ``user_preferences``
  value is ``false``, so ``status`` must still report ``False``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome import __version__
from genome.cli import app
from genome.db import init_databases
from genome.privacy.external_client import is_external_enabled

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


def _labeled_values(output: str, token: str) -> list[str]:
    """Return the stripped post-colon value of every ``status`` line with ``token``.

    ``status`` column-aligns with *variable* leading whitespace, so a naive
    ``"exists: False" in output`` check is unreliable — parse per line via
    ``line.split(":", 1)[1].strip()`` instead.
    """
    return [line.split(":", 1)[1].strip() for line in output.splitlines() if token in line]


def test_version_prints_package_version(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    # Sourced from genome.__version__, not hardcoded, so a bump can't drift this.
    assert result.output.strip() == __version__


def test_version_flag_prints_package_version(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    # Sourced from genome.__version__, not hardcoded, so a bump can't drift this.
    assert result.output.strip() == __version__


def test_version_flag_matches_version_subcommand(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    runner = CliRunner()
    flag_result = runner.invoke(app, ["--version"])
    subcommand_result = runner.invoke(app, ["version"])
    assert flag_result.exit_code == 0
    assert subcommand_result.exit_code == 0
    # Both surfaces resolve to the single genome.__version__ source.
    assert flag_result.output.strip() == subcommand_result.output.strip()


def test_bare_invocation_does_not_leak_version(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 2
    assert __version__ not in result.output


def test_root_app_registers_all_top_level_commands(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    # Unique value of this test: it locks the ``config`` sub-app GROUP registration.
    # The top-level command-name subset below is a secondary guard against a
    # top-level command silently being dropped.
    names = sorted(ci.name or ci.callback.__name__ for ci in app.registered_commands)
    assert {"init", "status", "version"}.issubset(names)
    assert "config" in {gi.name for gi in app.registered_groups if gi.name}
    result = CliRunner().invoke(app, ["--help"], env={"TERM": "dumb"})
    assert result.exit_code == 0


def test_init_creates_both_databases_reports_created(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    # Empty tmp (no prior init): both DBs are created this run.
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0
    assert result.output.count("created") == 2
    assert "present (skipped)" not in result.output


def test_init_idempotent_reports_present_skipped(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    runner = CliRunner()
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0
    # Second invocation on an already-initialized tmp: both DBs are skipped.
    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0
    assert second.output.count("present (skipped)") == 2
    assert "created" not in second.output


def test_status_before_init_reports_absent_and_fail_closed_external(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    exists_values = _labeled_values(result.output, "exists:")
    assert len(exists_values) == 2
    assert exists_values == ["False", "False"]
    # Both DBs are absent, so the exists-guarded count blocks never ran.
    assert "tables:" not in result.output
    # Fail-closed else at cli.py:184 — app.db absent => external reported False.
    assert "External calls enabled: False" in result.output


def test_status_after_init_reports_counts_and_seed_false_external(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    exists_values = _labeled_values(result.output, "exists:")
    assert exists_values == ["True", "True"]
    # The exists-guarded count blocks ran; do NOT pin exact counts (schema evolves).
    assert "tables:" in result.output
    assert "views:" in result.output
    profiles = _labeled_values(result.output, "profiles:")
    assert len(profiles) == 1
    assert int(profiles[0]) == 1
    preferences = _labeled_values(result.output, "preferences:")
    assert len(preferences) == 1
    assert int(preferences[0]) >= 12
    # Seed default: fresh init, no `config set` => user_preferences is 'false'.
    assert "External calls enabled: False" in result.output


def test_status_reports_false_when_env_true_but_pref_false(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finding-024 over-report discriminator: env says True, the gate says False.

    ``status`` reports the LIVE ``user_preferences`` gate value, not the ``.env``
    snapshot. With ``EXTERNAL_CALLS_ENABLED=true`` in the env but the seeded
    ``user_preferences.external_calls_enabled`` still ``false``, both the gate and
    ``status`` must report False. The pre-fix code (reading the ``.env`` Settings)
    would over-report True here.
    """
    monkeypatch.setenv("EXTERNAL_CALLS_ENABLED", "true")
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    # init seeds user_preferences.external_calls_enabled='false' regardless of env.
    init_databases()
    # Sanity: the egress gate reads user_preferences, so it ignores the env snapshot.
    assert is_external_enabled() is False
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert "External calls enabled: False" in result.output
