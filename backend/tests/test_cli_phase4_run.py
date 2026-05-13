"""CLI smoke tests for ``genome imputation run``.

The underlying Beagle runner is exercised in
``test_imputation_beagle_runner.py``; this file asserts the CLI wiring
(parsing, exit codes, the chromosome filter, and the summary output)
and mocks the runner to keep it hermetic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest
import structlog
from typer.testing import CliRunner

from genome.cli import app
from genome.imputation.beagle_runner import BeagleRunResult

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Restore structlog defaults after each test (mirrors test_cli_phase4)."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def test_run_help_lists_all_flags(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "run", "--help"])
    assert result.exit_code == 0
    for flag in ("--chromosomes", "--threads", "--memory-gb", "--ne", "--force"):
        assert flag in result.output, f"{flag!r} missing from `imputation run --help`"


def test_run_command_appears_in_parent_help(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    runner = CliRunner()
    top = runner.invoke(app, ["imputation", "--help"])
    assert top.exit_code == 0
    assert "run" in top.output


def test_run_calls_run_imputation_and_prints_summary(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI should delegate to ``run_imputation`` and surface the summary."""
    fake_result = BeagleRunResult(
        imputation_id=1,
        chromosomes_attempted=("1", "2"),
        chromosomes_completed=("1", "2"),
        chromosomes_failed=(),
        chromosomes_skipped=(),
        per_chrom_seconds={"1": 123.4, "2": 56.7},
    )
    run_mock = mock.MagicMock(return_value=fake_result)
    monkeypatch.setattr("genome.cli.run_imputation", run_mock)

    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "run", "1"])
    assert result.exit_code == 0, result.output
    assert "imputation_id=1" in result.output
    assert "completed=['1', '2']" in result.output
    assert "per_chrom_seconds:" in result.output
    assert "1=123.4s" in result.output
    # On success, the next-step pointer at the bottom mentions import.
    assert "import" in result.output.lower()
    # The CLI should call run_imputation with the positional id and no
    # filters set.
    run_mock.assert_called_once()
    _, kwargs = run_mock.call_args
    assert run_mock.call_args.args == (1,)
    assert kwargs["chromosomes"] is None
    assert kwargs["force"] is False


def test_run_passes_chromosomes_filter(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_result = BeagleRunResult(
        imputation_id=2,
        chromosomes_attempted=("X",),
        chromosomes_completed=("X",),
        chromosomes_failed=(),
        chromosomes_skipped=(),
        per_chrom_seconds={"X": 99.0},
    )
    run_mock = mock.MagicMock(return_value=fake_result)
    monkeypatch.setattr("genome.cli.run_imputation", run_mock)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["imputation", "run", "2", "--chromosomes", "chr1,X"],
    )
    assert result.exit_code == 0, result.output
    _, kwargs = run_mock.call_args
    assert kwargs["chromosomes"] == frozenset({"1", "X"})


def test_run_passes_threads_memory_ne_and_force(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_result = BeagleRunResult(
        imputation_id=3,
        chromosomes_attempted=("1",),
        chromosomes_completed=("1",),
        chromosomes_failed=(),
        chromosomes_skipped=(),
        per_chrom_seconds={"1": 10.0},
    )
    run_mock = mock.MagicMock(return_value=fake_result)
    monkeypatch.setattr("genome.cli.run_imputation", run_mock)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "imputation",
            "run",
            "3",
            "--threads",
            "4",
            "--memory-gb",
            "16",
            "--ne",
            "20000",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    _, kwargs = run_mock.call_args
    assert kwargs["threads"] == 4
    assert kwargs["memory_gb"] == 16
    assert kwargs["ne"] == 20000
    assert kwargs["force"] is True


def test_run_rejects_invalid_chromosome_filter(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["imputation", "run", "1", "--chromosomes", "1,FOO"],
    )
    assert result.exit_code != 0
    combined = result.output.lower() + str(result.exception or "").lower()
    assert "invalid chromosome" in combined


def test_run_failure_summary_points_at_retry(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_result = BeagleRunResult(
        imputation_id=4,
        chromosomes_attempted=("1", "2"),
        chromosomes_completed=("2",),
        chromosomes_failed=("1",),
        chromosomes_skipped=(),
        per_chrom_seconds={"1": 5.0, "2": 6.0},
    )
    run_mock = mock.MagicMock(return_value=fake_result)
    monkeypatch.setattr("genome.cli.run_imputation", run_mock)

    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "run", "4"])
    assert result.exit_code == 0
    # On failure the next-step pointer should suggest retrying the failed chroms.
    assert "--chromosomes 1" in result.output
