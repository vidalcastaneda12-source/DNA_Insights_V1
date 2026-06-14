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
import structlog

from genome.db import init_databases
from genome.db.sqlite_conn import sqlcipher_connection
from genome.imputation.reference_panel import (
    BEAGLE_JAR_URL,
    EXTERNAL_ENDPOINT_LABEL,
    GENETIC_MAP_URL,
    PANEL_CHROMOSOMES,
    ReferencePanel,
    _normalize_map_file,
    _normalize_on_disk_maps,
    _remove_doubled_map_files,
    default_panel_root,
    install_panel,
    normalize_map_chrom,
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
    """Build a minimal in-memory zip mimicking plink.GRCh38.map.zip.

    Mirrors the real upstream archive: column 1 of every line is the bare
    chromosome label (``22``, ``23``), without a ``chr`` prefix. The
    install step is expected to rewrite each extracted file in place so
    column 1 becomes ``chr``-prefixed.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in chroms:
            # PLINK chr labels Y as 24 and X as 23 in the upstream maps;
            # we only carry X here (chrY is intentionally absent from the
            # panel set), so we use ``23`` to mimic the upstream encoding
            # for that one chromosome.
            col1 = "23" if c == "X" else c
            zf.writestr(
                f"plink.chr{c}.GRCh38.map",
                f"{col1}\t.\t0.0\t1\n{col1}\t.\t1.0\t1000\n",
            )
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
    # chrX map column 1 must read 'chrX' (PR 5a validate_panel assertion).
    panel.map_for_chrom("X").write_bytes(b"chrX\t.\t0.0\t1\n")
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
        # Realistic column-1 label so the chrX-col1 assertion (PR 5a) passes.
        panel.map_for_chrom(c).write_bytes(f"chr{c}\t.\t0.0\t1\n".encode("ascii"))
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


# -----------------------------------------------------------------------------
# Genetic-map chr-prefix normalization (Beagle 5.5 compat)
# -----------------------------------------------------------------------------


def test_install_panel_rewrites_genetic_map_with_chr_prefix(
    panel_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],  # noqa: ARG001 — keep the patch active
) -> None:
    """The Browning Lab archive's column-1 is unprefixed; the install
    step must rewrite each extracted .map so column 1 carries ``chr``.

    Beagle 5.5 does exact-string chromosome matching and refuses to run
    when the genetic map's labels don't match the panel/input VCFs'
    ``chr``-prefixed labels.
    """
    init_databases()
    _enable_external_calls()
    panel = ReferencePanel.resolve()

    install_panel(panel)

    for c in PANEL_CHROMOSOMES:
        mfile = panel.map_for_chrom(c)
        assert mfile.is_file()
        # Every non-blank line's first column must be chr-prefixed.
        lines = mfile.read_text().splitlines()
        non_blank = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
        assert non_blank, f"no data lines for chr{c} in {mfile}"
        for line in non_blank:
            col1 = line.split("\t", 1)[0]
            assert col1.startswith("chr"), f"chr{c}: line column 1 is not chr-prefixed: {line!r}"
        # Permissions survive the in-place rewrite.
        assert stat.S_IMODE(mfile.stat().st_mode) == 0o600


def test_install_panel_chr_prefix_rewrite_is_idempotent(
    panel_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],  # noqa: ARG001
) -> None:
    """Re-running ``--force`` on already chr-prefixed maps leaves them byte-identical.

    Important because ``panel install`` is idempotent overall — a user
    who runs install twice (or once with ``--force``) must not see the
    prefix doubled (``chrchr22``) on the second pass.
    """
    init_databases()
    _enable_external_calls()
    panel = ReferencePanel.resolve()
    install_panel(panel)

    # Snapshot the rewritten files' bytes.
    pre_rewrite: dict[str, bytes] = {
        c: panel.map_for_chrom(c).read_bytes() for c in PANEL_CHROMOSOMES
    }

    # Force re-install. The map archive is re-extracted (because force=True
    # triggers needs_extract), then the prefix rewrite runs again. The
    # rewrite step is a no-op on already chr-prefixed files, but the
    # re-extraction from the zip writes the upstream (unprefixed) bytes
    # first — so the final result after the rewrite must still match the
    # first install's bytes.
    install_panel(panel, force=True)

    for c in PANEL_CHROMOSOMES:
        post = panel.map_for_chrom(c).read_bytes()
        assert post == pre_rewrite[c], f"chr{c}: rewrite is not idempotent across re-install"


def test_install_panel_chr_prefix_rewrite_preserves_existing_chr(
    panel_root: Path,  # noqa: ARG001 — fixture sets settings override
    mock_transport: dict[str, list[httpx.Request]],
) -> None:
    """A map archive that already ships chr-prefixed lines is left alone.

    Some future upstream release may switch the maps to chr-prefixed
    labels; the rewrite must be a no-op in that case rather than
    producing ``chrchr<N>``.
    """
    init_databases()
    _enable_external_calls()
    panel = ReferencePanel.resolve()

    # Swap the mock transport's map zip for one whose column 1 is already
    # chr-prefixed (and uses tabs, matching the real format).
    chr_prefixed_zip = io.BytesIO()
    with zipfile.ZipFile(chr_prefixed_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in PANEL_CHROMOSOMES:
            zf.writestr(
                f"plink.chr{c}.GRCh38.map",
                f"chr{c}\t.\t0.0\t1\nchr{c}\t.\t1.0\t1000\n",
            )
    real_handler = mock_transport  # keep reference

    # Replace the GENETIC_MAP_URL handler on the existing transport.
    # The fixture is keyed by the captured requests, so the simplest
    # path is to monkeypatch httpx.Client one more time. Instead, we
    # reuse the fixture's transport by directly seeding the archive
    # cache to bypass the download.
    panel.ensure_layout()
    panel.genetic_map_archive.write_bytes(chr_prefixed_zip.getvalue())
    panel.genetic_map_archive.chmod(0o600)
    # _build_map_zip_bytes-style: write the JAR + per-chrom VCFs via the
    # existing transport so we don't trip the no-network check.
    install_panel(panel)
    assert real_handler["requests"]  # the transport was exercised

    for c in PANEL_CHROMOSOMES:
        mfile = panel.map_for_chrom(c)
        text = mfile.read_text()
        # Must not have a doubled prefix anywhere.
        assert "chrchr" not in text
        # Column 1 starts with chr — single prefix.
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            col1 = line.split("\t", 1)[0]
            assert col1.startswith("chr")
            assert not col1.startswith("chrchr")


# -----------------------------------------------------------------------------
# Genetic-map column-1 normalization (PR 5a — Beagle exact-string matching).
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("col1", "expected"),
    [
        ("chrX", "chrX"),  # already canonical — a byte-identical no-op
        ("X", "chrX"),
        ("23", "chrX"),  # PLINK numeric sex label
        ("chr23", "chrX"),
        ("chrchrX", "chrX"),  # repair a doubled prefix
        ("1", "chr1"),
        ("22", "chr22"),
        ("chr1", "chr1"),
        ("chrchr1", "chr1"),
        ("24", "chrY"),  # PLINK numeric Y — not in the panel, but the map is total
        ("chr24", "chrY"),
    ],
)
def test_normalize_map_chrom_canonicalizes(col1: str, expected: str) -> None:
    assert normalize_map_chrom(col1) == expected


@pytest.mark.parametrize(
    "col1",
    ["chrX", "X", "23", "chr23", "chrchrX", "1", "chrchr1", "24"],
)
def test_normalize_map_chrom_is_a_fixed_point(col1: str) -> None:
    once = normalize_map_chrom(col1)
    assert normalize_map_chrom(once) == once
    assert not once.startswith("chrchr")


def test_normalize_map_file_rewrites_bare_and_doubled_labels(tmp_path: Path) -> None:
    log = structlog.get_logger("test")
    mapfile = tmp_path / "plink.chrX.GRCh38.map"
    mapfile.write_text("23\t.\t0.0\t1\n23\t.\t1.0\t1000\n")

    assert _normalize_map_file(mapfile, log) is True
    assert mapfile.read_text() == "chrX\t.\t0.0\t1\nchrX\t.\t1.0\t1000\n"

    # Second pass: nothing to do, byte-identical.
    before = mapfile.read_bytes()
    assert _normalize_map_file(mapfile, log) is False
    assert mapfile.read_bytes() == before


def test_normalize_map_file_passes_comments_and_blanks_through(tmp_path: Path) -> None:
    log = structlog.get_logger("test")
    mapfile = tmp_path / "plink.chr1.GRCh38.map"
    mapfile.write_text("# header comment\n\n1\t.\t0.0\t1\n")
    _normalize_map_file(mapfile, log)
    assert mapfile.read_text() == "# header comment\n\nchr1\t.\t0.0\t1\n"


def test_normalize_on_disk_maps_repairs_existing(
    panel_root: Path,  # noqa: ARG001
) -> None:
    log = structlog.get_logger("test")
    panel = ReferencePanel.resolve()
    panel.ensure_layout()
    for c in PANEL_CHROMOSOMES:
        col1 = "23" if c == "X" else c
        panel.map_for_chrom(c).write_text(f"{col1}\t.\t0.0\t1\n")

    _normalize_on_disk_maps(panel, log)

    assert panel.map_for_chrom("X").read_text().split("\t", 1)[0] == "chrX"
    assert panel.map_for_chrom("1").read_text().split("\t", 1)[0] == "chr1"


def test_remove_doubled_map_files(
    panel_root: Path,  # noqa: ARG001
) -> None:
    log = structlog.get_logger("test")
    panel = ReferencePanel.resolve()
    panel.ensure_layout()
    good = panel.map_for_chrom("1")
    good.write_text("chr1\t.\t0.0\t1\n")
    stray_x = panel.genetic_map_dir / "plink.chrchrX.GRCh38.map"
    stray_1 = panel.genetic_map_dir / "plink.chrchr1.GRCh38.map"
    stray_x.write_text("junk")
    stray_1.write_text("junk")

    _remove_doubled_map_files(panel, log)

    assert not stray_x.exists()
    assert not stray_1.exists()
    assert good.exists()


def test_validate_panel_flags_wrong_chrx_map_col1(
    panel_root: Path,  # noqa: ARG001
) -> None:
    panel = ReferencePanel.resolve()
    panel.ensure_layout()
    panel.beagle_jar.write_bytes(b"jar")
    for c in PANEL_CHROMOSOMES:
        # chrX carries the WRONG (bare PLINK numeric) label; the rest are fine.
        col1 = "23" if c == "X" else f"chr{c}"
        panel.map_for_chrom(c).write_text(f"{col1}\t.\t0.0\t1\n")
        p = panel.panel_for_chrom(c)
        assert p is not None
        p.write_bytes(b"v")

    problems = validate_panel(panel)
    assert any("chrX genetic map column 1" in p for p in problems)


def test_install_maps_chrx_label_to_chrx(
    panel_root: Path,  # noqa: ARG001
    mock_transport: dict[str, list[httpx.Request]],  # noqa: ARG001 — keeps the patch active
) -> None:
    """End-to-end: install rewrites the chrX map's bare ``23`` to ``chrX``."""
    init_databases()
    _enable_external_calls()
    panel = ReferencePanel.resolve()

    install_panel(panel)

    chrx_col1 = panel.map_for_chrom("X").read_text().splitlines()[0].split("\t", 1)[0]
    assert chrx_col1 == "chrX"
    # A real install therefore satisfies the validate_panel chrX assertion.
    assert validate_panel(panel) == []
