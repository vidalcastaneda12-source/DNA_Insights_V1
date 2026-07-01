"""CLI smoke tests for ``genome imputation register-existing-result``.

Authored blind to the implementation diff (Stage-2 test-author): the core
``register_existing_result`` behavior lives in ``test_imputation_register.py``;
here the runner is mocked so these assert only the CLI wiring — help visibility,
delegation + summary, and the fail-closed exit code — from the frozen interface
and plan §5 / step 5. Mirrors ``test_cli_phase4_run.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest
import structlog
from typer.testing import CliRunner

from genome.cli import app
from genome.imputation import RegisterError, RegisterResult

if TYPE_CHECKING:
    from collections.abc import Iterator

    from click.testing import Result


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Restore structlog defaults after each test (mirrors test_cli_phase4_run)."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _combined_output(result: Result) -> str:
    """stdout + stderr, tolerant of click versions that fold vs separate stderr."""
    try:
        return result.output + result.stderr
    except (ValueError, AttributeError):
        # Older click folds stderr into ``output`` and raises on ``.stderr`` access.
        return result.output


def test_register_help_and_in_parent_help(
    isolated_settings: dict[str, str],  # noqa: ARG001 — hermetic settings only
) -> None:
    """from: plan §5 'register_help_and_in_parent_help' + step 5 (command name)."""
    runner = CliRunner()
    sub = runner.invoke(
        app,
        ["imputation", "register-existing-result", "--help"],
        env={"TERM": "dumb"},
    )
    assert sub.exit_code == 0
    top = runner.invoke(app, ["imputation", "--help"])
    assert top.exit_code == 0
    assert "register-existing-result" in top.output


def test_cli_delegates_and_prints_summary(
    isolated_settings: dict[str, str],  # noqa: ARG001 — hermetic settings only
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: plan §5 'cli_delegates_and_prints_summary' + step 5.

    The command delegates to ``register_existing_result(id)`` and, on success, prints the
    before→after transition and the ``genome imputation import <id>`` next-step; exit 0.
    """
    fake = RegisterResult(
        imputation_id=2,
        status_before="pending",
        status_after="completed",
        chromosomes_expected=("1", "2", "X"),
        chromosomes_validated=("1", "2", "X"),
        submitted_at="2026-07-01 00:00:00",
        completed_at="2026-07-01 00:00:05",
    )
    register_mock = mock.MagicMock(return_value=fake)
    monkeypatch.setattr("genome.cli.register_existing_result", register_mock)

    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "register-existing-result", "2"])

    assert result.exit_code == 0, result.output
    register_mock.assert_called_once()
    assert register_mock.call_args.args == (2,)
    # Summary surfaces the before->after transition ...
    assert "pending" in result.output
    assert "completed" in result.output
    # ... and points at the next step (genome imputation import <id>).
    assert "import" in result.output.lower()


def test_cli_refusal_exits_nonzero(
    isolated_settings: dict[str, str],  # noqa: ARG001 — hermetic settings only
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from: plan §5 'cli_refusal_exits_nonzero' + step 5.

    A ``RegisterError`` from the delegate is caught and re-surfaced as a non-zero exit with
    the message on stderr and NO success summary (fail-closed; chrx-loo echo(err)+Exit(1)).
    """
    register_mock = mock.MagicMock(
        side_effect=RegisterError("refusing to launder a failed run"),
    )
    monkeypatch.setattr("genome.cli.register_existing_result", register_mock)

    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "register-existing-result", "3"])

    assert result.exit_code == 1
    assert "launder" in _combined_output(result).lower()
    # No success summary / next-step pointer is printed on a refusal.
    assert "next step" not in result.output.lower()
