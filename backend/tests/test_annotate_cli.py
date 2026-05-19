"""CLI smoke tests for ``genome annotate`` subcommands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog
from typer.testing import CliRunner

from genome.annotate.registry import _clear_loaders_for_testing
from genome.annotate.source_versions import KNOWN_SOURCE_DBS, insert_source_version
from genome.annotate.supersession import flip_to_new_version
from genome.cli import app
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Restore structlog defaults after each test (mirrors test_cli_phase4)."""
    try:
        yield
    finally:
        structlog.reset_defaults()


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    _clear_loaders_for_testing()


@pytest.fixture
def annotations_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> Iterator[Path]:
    """Point ``settings.annotations_download_root`` at a tmp directory."""
    root = tmp_path / "annotations-root"
    monkeypatch.setenv("ANNOTATIONS_DOWNLOAD_ROOT", str(root))
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()


def test_annotate_help_lists_refresh_and_status(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "--help"])
    assert result.exit_code == 0
    assert "status" in result.output
    assert "refresh" in result.output


def test_annotate_status_prints_all_known_sources_as_not_loaded_on_fresh_db(
    annotations_root: Path,
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "status"])
    assert result.exit_code == 0, result.output
    for source in sorted(KNOWN_SOURCE_DBS):
        assert source in result.output
        # Each source line ends in "not loaded".
        matching = [line for line in result.output.splitlines() if line.startswith(f"{source}:")]
        assert matching, f"no status line for {source}: {result.output!r}"
        assert "not loaded" in matching[0]
    # Sources are listed in alphabetical order.
    listed = [
        line.split(":", 1)[0]
        for line in result.output.splitlines()
        if line and ":" in line and line.split(":", 1)[0] in KNOWN_SOURCE_DBS
    ]
    assert listed == sorted(KNOWN_SOURCE_DBS)
    # The on-disk cache directory must not have been created.
    assert not annotations_root.exists()


def test_annotate_status_reports_loaded_source_with_metadata(
    annotations_root: Path,  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        sv_id = insert_source_version(
            conn,
            source_db="clinvar",
            version="2026_04_15",
            source_url=None,
            source_file_hash="a" * 64,
            source_file_size=12_345,
            record_count=24,
        )
        # Seed one clinvar_annotations row so the flip's COUNT(*) is happy.
        conn.execute(
            """
            INSERT INTO clinvar_annotations (
                clinvar_id, variation_id, source_version_id, retrieval_date
            )
            VALUES (1, 'VCV1', ?, CURRENT_TIMESTAMP)
            """,
            [sv_id],
        )
        flip_to_new_version(
            conn,
            source="clinvar",
            table="clinvar_annotations",
            new_source_version_id=sv_id,
        )
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "status"])
    assert result.exit_code == 0, result.output
    clinvar_lines = [line for line in result.output.splitlines() if line.startswith("clinvar:")]
    assert len(clinvar_lines) == 1
    line = clinvar_lines[0]
    assert "2026_04_15" in line
    assert "ingested " in line
    assert "24 records" in line
    # The other ten sources still print as "not loaded".
    for source in sorted(KNOWN_SOURCE_DBS - {"clinvar"}):
        matches = [s for s in result.output.splitlines() if s.startswith(f"{source}:")]
        assert matches
        assert "not loaded" in matches[0]


def test_annotate_refresh_exits_2_with_stub_for_unregistered_source(
    annotations_root: Path,  # noqa: ARG001
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--source", "clinvar"])
    assert result.exit_code == 2
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "no loader registered" in combined
    assert "clinvar" in combined
    assert "5.0" in combined or "scaffold" in combined


def test_annotate_refresh_exits_2_for_unknown_source(
    annotations_root: Path,  # noqa: ARG001
) -> None:
    """An unknown source surfaces the same stub message (5.0 keeps the surface minimal)."""
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--source", "not_a_real_source"])
    assert result.exit_code == 2
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "no loader registered" in combined
    assert "not_a_real_source" in combined


def test_annotate_status_does_not_create_cache_directory(
    annotations_root: Path,
) -> None:
    """Regression guard for the "create only on download" decision."""
    init_databases()
    runner = CliRunner()
    runner.invoke(app, ["annotate", "status"])
    runner.invoke(app, ["annotate", "refresh", "--source", "clinvar"])
    assert not annotations_root.exists()


def test_annotate_refresh_help_documents_skip_if_same_version_flag(
    annotations_root: Path,  # noqa: ARG001
) -> None:
    """The new --skip-if-same-version flag must appear in --help output.

    Regression guard for the finding-009 #14 CLI surface: future readers
    can discover the flag without diffing the source.
    """
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--help"])
    assert result.exit_code == 0
    assert "--skip-if-same-version" in result.output


def test_annotate_refresh_skip_if_same_version_flag_reaches_loader(
    annotations_root: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--skip-if-same-version`` on the CLI must reach the loader call."""
    init_databases()
    received: dict[str, bool] = {}

    def _recording_refresh(
        force: bool,  # noqa: FBT001
        skip_if_same_version: bool,  # noqa: FBT001
    ) -> object:
        received["force"] = force
        received["skip_if_same_version"] = skip_if_same_version
        from genome.annotate.registry import RefreshResult  # noqa: PLC0415

        return RefreshResult(
            source_db="clinvar",
            source_version_id=1,
            version="2026_05_10",
            record_count=0,
            was_already_current=True,
        )

    monkeypatch.setattr(
        "genome.annotate.registry._LOADERS",
        {"clinvar": _recording_refresh},
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "annotate",
            "refresh",
            "--source",
            "clinvar",
            "--force",
            "--skip-if-same-version",
        ],
    )
    assert result.exit_code == 0, result.output
    assert received == {"force": True, "skip_if_same_version": True}


def test_annotate_refresh_without_skip_flag_passes_false_to_loader(
    annotations_root: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default behaviour: omitting --skip-if-same-version passes False through."""
    init_databases()
    received: dict[str, bool] = {}

    def _recording_refresh(
        force: bool,  # noqa: FBT001
        skip_if_same_version: bool,  # noqa: FBT001
    ) -> object:
        received["force"] = force
        received["skip_if_same_version"] = skip_if_same_version
        from genome.annotate.registry import RefreshResult  # noqa: PLC0415

        return RefreshResult(
            source_db="clinvar",
            source_version_id=1,
            version="2026_05_10",
            record_count=0,
            was_already_current=False,
        )

    monkeypatch.setattr(
        "genome.annotate.registry._LOADERS",
        {"clinvar": _recording_refresh},
    )

    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "refresh", "--source", "clinvar"])
    assert result.exit_code == 0, result.output
    assert received == {"force": False, "skip_if_same_version": False}
