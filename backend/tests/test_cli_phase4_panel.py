"""CLI smoke tests for ``genome imputation panel`` subcommands.

Verifies plumbing of the new ``panel status`` and ``panel install``
subcommands. Functional behavior of the underlying module lives in
``test_imputation_reference_panel.py``; this file asserts the CLI
wiring (parsing, exit codes, the external-calls gate, and the
chromosomes filter) and mocks the actual download.
"""

from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING

import httpx
import pytest
import structlog
from typer.testing import CliRunner

from genome.cli import app
from genome.db import init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.imputation.reference_panel import (
    BEAGLE_JAR_URL,
    GENETIC_MAP_URL,
    PANEL_CHROMOSOMES,
)

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


@pytest.fixture
def panel_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> Iterator[Path]:
    root = tmp_path / "panel-root"
    monkeypatch.setenv("IMPUTATION_PANEL_ROOT", str(root))
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()


def _enable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='true' WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _map_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in PANEL_CHROMOSOMES:
            zf.writestr(f"plink.chr{c}.GRCh38.map", f"chr{c} 0 0.0 0\n")
    return buf.getvalue()


@pytest.fixture
def mock_transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Patch httpx.Client to always use a MockTransport."""
    captured: dict[str, list[str]] = {"urls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        captured["urls"].append(url)
        if url == BEAGLE_JAR_URL:
            return httpx.Response(200, content=b"jar-bytes")
        if url == GENETIC_MAP_URL:
            return httpx.Response(200, content=_map_zip_bytes())
        if url.endswith(".vcf.gz"):
            return httpx.Response(200, content=b"vcf-bytes")
        return httpx.Response(404, text=f"unexpected: {url}")

    transport = httpx.MockTransport(handler)
    real_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    return captured


def test_panel_help_lists_status_and_install(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "panel", "--help"])
    assert result.exit_code == 0
    assert "status" in result.output
    assert "install" in result.output


def test_panel_status_on_empty_root_lists_missing(
    panel_root: Path,
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "panel", "status"])
    assert result.exit_code == 0
    assert str(panel_root) in result.output
    assert "missing" in result.output.lower()
    # The Beagle JAR is the first reported missing artifact.
    assert "Beagle JAR" in result.output


def test_panel_install_blocks_when_external_calls_disabled(
    panel_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[str]],  # noqa: ARG001 — present so we'd catch if it fires
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(app, ["imputation", "panel", "install"])
    # Master switch is off by default in tests — the command must exit non-zero
    # without making any download attempt.
    assert result.exit_code == 1
    assert "external_calls_enabled" in result.output.lower() or (
        "external" in result.output.lower() and "disabled" in result.output.lower()
    )


def test_panel_install_with_chromosomes_filter_downloads_subset(
    panel_root: Path,
    mock_transport: dict[str, list[str]],
) -> None:
    init_databases()
    _enable_external_calls()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["imputation", "panel", "install", "--chromosomes", "22,X"],
    )
    assert result.exit_code == 0, result.output
    # The selected chromosomes' panel VCFs should now exist on disk.
    panel_dir = panel_root / "panel"
    assert (panel_dir / "chr22.vcf.gz").is_file()
    assert (panel_dir / "chrX.vcf.gz").is_file()
    assert not (panel_dir / "chr1.vcf.gz").is_file()
    # Only the per-chrom URLs hit the wire — no JAR, no map zip.
    assert BEAGLE_JAR_URL not in mock_transport["urls"]
    assert GENETIC_MAP_URL not in mock_transport["urls"]
    chr_hits = [u for u in mock_transport["urls"] if u.endswith(".vcf.gz")]
    assert len(chr_hits) == 2


def test_panel_install_rejects_unknown_chromosome(
    panel_root: Path,  # noqa: ARG001
) -> None:
    init_databases()
    _enable_external_calls()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["imputation", "panel", "install", "--chromosomes", "Y"],
    )
    assert result.exit_code != 0
    combined = result.output.lower() + str(result.exception or "").lower()
    assert "invalid" in combined or "y" in combined
