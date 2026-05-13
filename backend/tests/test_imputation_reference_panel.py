"""Tests for :mod:`genome.imputation.reference_panel`.

Covers the contract the Beagle runner and the updated import workflow
depend on: the on-disk layout is deterministic, validation reports
missing artifacts accurately, and install routes every download through
the audited external HTTP client (which gates on the master switch and
records audit rows).
"""

from __future__ import annotations

import io
import json
import stat
import zipfile
from typing import TYPE_CHECKING

import httpx
import pytest

from genome.db import init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.imputation.reference_panel import (
    BEAGLE_JAR_URL,
    EXTERNAL_ENDPOINT_LABEL,
    GENETIC_MAP_URL,
    PANEL_CHROMOSOMES,
    ReferencePanel,
    default_panel_root,
    install_panel,
    validate_panel,
)
from genome.privacy.external_client import ExternalCallsDisabledError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _enable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='true' WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _disable_external_calls() -> None:
    with sqlcipher_connection() as conn:
        conn.execute(
            "UPDATE user_preferences SET pref_value='false'"
            " WHERE pref_key='external_calls_enabled'",
        )
        conn.commit()


def _audit_rows() -> list[tuple[object, ...]]:
    with sqlcipher_connection() as conn:
        return conn.execute(
            "SELECT action_type, resource_type, resource_id, operation_details,"
            " external_call, external_endpoint, external_payload_hash"
            " FROM audit_log ORDER BY log_id",
        ).fetchall()


def _build_map_zip_bytes(chroms: list[str]) -> bytes:
    """Build a minimal in-memory zip mimicking plink.GRCh38.map.zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in chroms:
            zf.writestr(f"plink.chr{c}.GRCh38.map", f"chr{c} 0 0.0 0\n")
    return buf.getvalue()


@pytest.fixture
def panel_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings: dict[str, str],  # noqa: ARG001 — sets up env + cached settings
) -> Iterator[Path]:
    """Point ``settings.imputation_panel_root`` at a tmp directory."""
    root = tmp_path / "panel-root"
    monkeypatch.setenv("IMPUTATION_PANEL_ROOT", str(root))
    from genome.config import get_settings  # noqa: PLC0415 — late import after env

    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()


@pytest.fixture
def mock_transport(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[httpx.Request]]:
    """Patch httpx.Client so every constructed client uses a MockTransport.

    The returned dict captures requests, keyed by ``"requests"``. Each
    handler entry is the full URL string the handler matched.
    """
    captured: dict[str, list[httpx.Request]] = {"requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["requests"].append(request)
        url = str(request.url)
        if url == BEAGLE_JAR_URL:
            return httpx.Response(200, content=b"BEAGLE_JAR_BYTES")
        if url == GENETIC_MAP_URL:
            map_zip = _build_map_zip_bytes(sorted(PANEL_CHROMOSOMES))
            return httpx.Response(200, content=map_zip)
        if "chr" in url and url.endswith(".vcf.gz"):
            return httpx.Response(200, content=b"VCF_BYTES_" + url.encode()[-20:])
        return httpx.Response(404, text="unexpected URL: " + url)

    transport = httpx.MockTransport(handler)
    real_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)
    return captured


# -----------------------------------------------------------------------------
# default_panel_root
# -----------------------------------------------------------------------------


def test_default_panel_root_falls_back_to_home_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an override, the default root lives under ``~/.cache/...``."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Some pydantic-settings setups read APP_DB_PASSPHRASE; satisfy it.
    monkeypatch.setenv("APP_DB_PASSPHRASE", "x")
    monkeypatch.delenv("IMPUTATION_PANEL_ROOT", raising=False)
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        root = default_panel_root()
    finally:
        get_settings.cache_clear()
    assert root == fake_home / ".cache" / "genome" / "imputation"


def test_default_panel_root_respects_settings_override(
    panel_root: Path,
) -> None:
    assert default_panel_root() == panel_root


# -----------------------------------------------------------------------------
# ReferencePanel.resolve
# -----------------------------------------------------------------------------


def test_resolve_produces_expected_paths(panel_root: Path) -> None:
    panel = ReferencePanel.resolve()
    assert panel.root == panel_root
    assert panel.beagle_jar.parent == panel_root
    assert panel.beagle_jar.name.startswith("beagle.")
    assert panel.beagle_jar.suffix == ".jar"
    assert panel.genetic_map_dir == panel_root / "genetic_maps"
    # Autosomes 1 and 22 and sex chromosome X are present; Y is intentionally absent.
    assert panel.panel_for_chrom("1") == panel_root / "panel" / "chr1.vcf.gz"
    assert panel.panel_for_chrom("22") == panel_root / "panel" / "chr22.vcf.gz"
    assert panel.panel_for_chrom("X") == panel_root / "panel" / "chrX.vcf.gz"
    assert panel.panel_for_chrom("Y") is None


def test_resolve_with_explicit_root_ignores_settings(
    tmp_path: Path,
    panel_root: Path,  # noqa: ARG001 — fixture sets settings override
) -> None:
    other = tmp_path / "elsewhere"
    panel = ReferencePanel.resolve(other)
    assert panel.root == other
    assert panel.beagle_jar.parent == other


def test_map_for_chrom_uses_plink_chr_naming(
    panel_root: Path,  # noqa: ARG001
) -> None:
    panel = ReferencePanel.resolve()
    assert panel.map_for_chrom("1").name == "plink.chr1.GRCh38.map"
    assert panel.map_for_chrom("X").name == "plink.chrX.GRCh38.map"


# -----------------------------------------------------------------------------
# validate_panel
# -----------------------------------------------------------------------------


def test_validate_panel_reports_everything_missing_on_empty_root(
    panel_root: Path,  # noqa: ARG001
) -> None:
    panel = ReferencePanel.resolve()
    problems = validate_panel(panel)
    # Expect: 1 JAR + len(PANEL_CHROMOSOMES) maps + len(PANEL_CHROMOSOMES) panel VCFs.
    assert len(problems) == 1 + 2 * len(PANEL_CHROMOSOMES)
    # The JAR is reported first.
    assert "Beagle JAR" in problems[0]


def test_validate_panel_partial_install_reports_only_missing(
    panel_root: Path,  # noqa: ARG001
) -> None:
    panel = ReferencePanel.resolve()
    panel.ensure_layout()
    # Plant the JAR + some maps + some panels.
    panel.beagle_jar.write_bytes(b"jar")
    panel.map_for_chrom("1").write_bytes(b"map1")
    panel.map_for_chrom("X").write_bytes(b"mapX")
    p1 = panel.panel_for_chrom("1")
    assert p1 is not None
    p1.write_bytes(b"vcf1")
    problems = validate_panel(panel)
    # 1 JAR is present; (chroms - 2) maps missing; (chroms - 1) panel VCFs missing.
    n = len(PANEL_CHROMOSOMES)
    expected_missing = (n - 2) + (n - 1)
    assert len(problems) == expected_missing
    # The present items should not appear.
    joined = "\n".join(problems)
    assert "plink.chr1.GRCh38.map" not in joined
    assert "plink.chrX.GRCh38.map" not in joined
    assert "chr1.vcf.gz" not in joined


def test_validate_panel_complete_install_returns_empty(
    panel_root: Path,  # noqa: ARG001
) -> None:
    panel = ReferencePanel.resolve()
    panel.ensure_layout()
    panel.beagle_jar.write_bytes(b"jar")
    for c in PANEL_CHROMOSOMES:
        panel.map_for_chrom(c).write_bytes(b"m")
        p = panel.panel_for_chrom(c)
        assert p is not None
        p.write_bytes(b"v")
    assert validate_panel(panel) == []


# -----------------------------------------------------------------------------
# install_panel — happy path / disabled switch / chromosomes filter
# -----------------------------------------------------------------------------


def test_install_panel_downloads_everything_and_writes_audit(
    panel_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],
) -> None:
    init_databases()
    _enable_external_calls()
    panel = ReferencePanel.resolve()

    install_panel(panel)

    # Beagle JAR + map archive + every per-chrom VCF are now on disk with 0600.
    assert panel.beagle_jar.is_file()
    assert stat.S_IMODE(panel.beagle_jar.stat().st_mode) == 0o600
    assert panel.genetic_map_archive.is_file()
    assert stat.S_IMODE(panel.genetic_map_archive.stat().st_mode) == 0o600
    # The map zip's contents were extracted into genetic_map_dir.
    for c in PANEL_CHROMOSOMES:
        mfile = panel.map_for_chrom(c)
        assert mfile.is_file(), f"missing extracted map for chr{c}"
        assert stat.S_IMODE(mfile.stat().st_mode) == 0o600
    for c in PANEL_CHROMOSOMES:
        p = panel.panel_for_chrom(c)
        assert p is not None
        assert p.is_file()
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    # Every URL went through the patched transport.
    seen_urls = {str(r.url) for r in mock_transport["requests"]}
    assert BEAGLE_JAR_URL in seen_urls
    assert GENETIC_MAP_URL in seen_urls
    for c in PANEL_CHROMOSOMES:
        # The per-chrom panel URL includes the chromosome label.
        matches = [u for u in seen_urls if f"chr{c}." in u and u.endswith(".vcf.gz")]
        assert matches, f"no panel URL fetched for chr{c}"

    # Audit rows are present: 2 per download (intent + result) and one row
    # per artifact must reference resource_type='reference_panel'.
    rows = _audit_rows()
    # Beagle JAR (2) + map (2) + every chrom (2 each).
    expected_rows = 2 + 2 + 2 * len(PANEL_CHROMOSOMES)
    assert len(rows) == expected_rows, rows
    panel_rows = [r for r in rows if r[1] == "reference_panel"]
    assert len(panel_rows) == expected_rows
    endpoints = {r[5] for r in panel_rows}
    assert endpoints == {EXTERNAL_ENDPOINT_LABEL}
    resource_ids = {r[2] for r in panel_rows}
    assert "jar" in resource_ids
    assert "map" in resource_ids
    for c in PANEL_CHROMOSOMES:
        assert c in resource_ids, f"missing audit row for chr{c}"


def test_install_panel_blocks_when_external_calls_disabled(
    panel_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],  # noqa: ARG001 — keep the patch active
) -> None:
    init_databases()
    _disable_external_calls()
    panel = ReferencePanel.resolve()

    with pytest.raises(ExternalCallsDisabledError):
        install_panel(panel)

    # The Beagle JAR is the first download attempted; nothing else should
    # have hit the network because the first call raises.
    assert not panel.beagle_jar.is_file()
    # Audit log has intent + blocked rows for the first attempt only.
    rows = _audit_rows()
    assert len(rows) == 2
    intent, blocked = rows
    intent_details = json.loads(str(intent[3]))
    blocked_details = json.loads(str(blocked[3]))
    assert intent_details["phase"] == "intent"
    assert blocked_details["status"] == "blocked"
    assert intent[1] == blocked[1] == "reference_panel"
    assert intent[2] == blocked[2] == "jar"


def test_install_panel_chromosomes_filter_downloads_only_subset(
    panel_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],  # noqa: ARG001 — keep the patch active
) -> None:
    init_databases()
    _enable_external_calls()
    panel = ReferencePanel.resolve()

    install_panel(panel, chromosomes=frozenset({"22", "X"}))

    # Only the two requested per-chromosome panels were downloaded.
    panel_22 = panel.panel_for_chrom("22")
    panel_x = panel.panel_for_chrom("X")
    panel_1 = panel.panel_for_chrom("1")
    assert panel_22 is not None
    assert panel_22.is_file()
    assert panel_x is not None
    assert panel_x.is_file()
    assert panel_1 is not None
    assert not panel_1.is_file()

    # JAR and genetic map are NOT downloaded when a chromosomes filter is set.
    assert not panel.beagle_jar.is_file()
    assert not panel.genetic_map_archive.is_file()

    # Audit rows: 2 downloads, 2 rows each.
    rows = _audit_rows()
    assert len(rows) == 4
    resource_ids = sorted({r[2] for r in rows})
    assert resource_ids == ["22", "X"]


def test_install_panel_is_idempotent_when_files_exist(
    panel_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],
) -> None:
    """Re-running install_panel with everything in place issues no downloads."""
    init_databases()
    _enable_external_calls()
    panel = ReferencePanel.resolve()
    # First install populates the panel.
    install_panel(panel)
    initial_requests = list(mock_transport["requests"])
    # Second install should be a no-op (no new requests).
    install_panel(panel)
    assert len(mock_transport["requests"]) == len(initial_requests)


def test_install_panel_force_redownloads_existing_files(
    panel_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],
) -> None:
    init_databases()
    _enable_external_calls()
    panel = ReferencePanel.resolve()
    install_panel(panel, chromosomes=frozenset({"22"}))
    first = len(mock_transport["requests"])
    # Force re-download just chr22.
    install_panel(panel, chromosomes=frozenset({"22"}), force=True)
    assert len(mock_transport["requests"]) == first + 1


def test_install_panel_rejects_unknown_chromosome(
    panel_root: Path,  # noqa: ARG001
) -> None:
    init_databases()
    panel = ReferencePanel.resolve()
    with pytest.raises(ValueError, match="unknown panel chromosome"):
        install_panel(panel, chromosomes=frozenset({"Y"}))
