"""Tests for the M3-physical chrX panel region split + R1 re-diploidizer (PR 5a, finding-029).

These exercise the *real* ``bcftools`` region split and ``bgzip``/``awk``
re-diploidize transform on small synthetic BGZF chrX VCFs (skipped when any tool
is absent), so what runs in production is what is under test. They pin: the
PAR1 / non-PAR / PAR2 partition (including both telomeric slivers landing in
non-PAR), the PAR-haploid-free + non-PAR-retains-males composition assertions,
the panel index being ensured, the un-gated idempotent re-diploidizer
(``0`` → ``0|0``, GT:DS preservation, no-op on diploid), and the CLI region-panel
pre-flight gate.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from genome.cli import _require_chrx_region_panels
from genome.imputation.chrx_panel import (
    ChrxToolingError,
    count_haploid_gts,
    has_haploid_gt,
    prepare_chrx_panel,
    rediploidize_vcf,
)
from genome.imputation.reference_panel import ReferencePanel

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    shutil.which("bcftools") is None
    or shutil.which("bgzip") is None
    or shutil.which("awk") is None,
    reason="bcftools, bgzip and awk are required for the M3 chrX panel path",
)

_BCFTOOLS = shutil.which("bcftools") or "bcftools"
_BGZIP = shutil.which("bgzip") or "bgzip"
_AWK = shutil.which("awk") or "awk"

# A ``##contig`` line is required so ``bcftools index -t`` (tabix) can index the
# synthetic panel for the region split.
_VCF_HEADER = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=chrX>\n"
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tMALE\tFEMALE\n"
)


def _bgzip_vcf(text: str, path: Path) -> None:
    proc = subprocess.run(  # noqa: S603
        [_BGZIP, "-c"],
        input=text.encode("ascii"),
        capture_output=True,
        check=True,
    )
    path.write_bytes(proc.stdout)


def _bgunzip(path: Path) -> str:
    proc = subprocess.run(  # noqa: S603
        [_BGZIP, "-dc", str(path)],
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode("ascii")


def _data_records(text: str) -> list[tuple[int, str, str]]:
    """Parse ``(pos, male_gt, female_gt)`` from non-header VCF lines."""
    out: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        f = line.split("\t")
        out.append((int(f[1]), f[9], f[10]))
    return out


def _records_vcf(rows: list[tuple[int, str, str]]) -> str:
    body = "".join(
        f"chrX\t{pos}\t.\tA\tG\t.\tPASS\t.\tGT\t{male}\t{female}\n" for pos, male, female in rows
    )
    return _VCF_HEADER + body


def _positions(path: Path) -> set[int]:
    return {pos for pos, _m, _f in _data_records(_bgunzip(path))}


def _panel_with_chrx(root: Path, vcf_text: str) -> ReferencePanel:
    panel = ReferencePanel.resolve(root)
    panel.ensure_layout()
    chrx = panel.panel_for_chrom("X")
    assert chrx is not None
    _bgzip_vcf(vcf_text, chrx)
    return panel


def _split(panel: ReferencePanel, **kwargs: object) -> object:
    return prepare_chrx_panel(
        panel,
        bcftools_bin=_BCFTOOLS,
        bgzip_bin=_BGZIP,
        awk_bin=_AWK,
        **kwargs,  # type: ignore[arg-type]
    )


# Mirrors the real 1000G panel ploidy: males diploid in PAR, haploid in non-PAR
# (including both telomeric slivers); females diploid throughout.
_REALISTIC_ROWS = [
    (5_000, "1", "0|1"),  # non-PAR lower sliver (< PAR1) — male haploid
    (1_000_000, "0|1", "0|0"),  # PAR1 — male diploid
    (50_000_000, "0", "1|1"),  # non-PAR core — male haploid
    (155_800_000, "1|0", "0|0"),  # PAR2 — male diploid
    (156_035_000, "1", "0|0"),  # non-PAR upper sliver (> PAR2) — male haploid
]


# ---------------------------------------------------------------------------
# prepare_chrx_panel — region split.
# ---------------------------------------------------------------------------


def test_region_split_partitions_into_three_subsets(tmp_path: Path) -> None:
    panel = _panel_with_chrx(tmp_path / "panel", _records_vcf(_REALISTIC_ROWS))

    result = _split(panel)

    assert result.skipped is False  # type: ignore[attr-defined]
    # Three native subsets exist and are the panel's region-property paths.
    assert result.par1_path == panel.chrx_par1_panel  # type: ignore[attr-defined]
    assert result.nonpar_path == panel.chrx_nonpar_panel  # type: ignore[attr-defined]
    assert result.par2_path == panel.chrx_par2_panel  # type: ignore[attr-defined]
    for p in (panel.chrx_par1_panel, panel.chrx_nonpar_panel, panel.chrx_par2_panel):
        assert p.is_file()

    # The partition: PAR positions to their PAR subset; both slivers + core to non-PAR.
    assert _positions(panel.chrx_par1_panel) == {1_000_000}
    assert _positions(panel.chrx_par2_panel) == {155_800_000}
    assert _positions(panel.chrx_nonpar_panel) == {5_000, 50_000_000, 156_035_000}


def test_region_split_par_haploid_free_nonpar_retains_males(tmp_path: Path) -> None:
    panel = _panel_with_chrx(tmp_path / "panel", _records_vcf(_REALISTIC_ROWS))

    result = _split(panel)

    # PAR subsets are diploid in both sexes; non-PAR keeps the male haploids.
    assert count_haploid_gts(panel.chrx_par1_panel, bgzip_bin=_BGZIP, awk_bin=_AWK) == 0
    assert count_haploid_gts(panel.chrx_par2_panel, bgzip_bin=_BGZIP, awk_bin=_AWK) == 0
    nonpar_haploid = count_haploid_gts(panel.chrx_nonpar_panel, bgzip_bin=_BGZIP, awk_bin=_AWK)
    assert nonpar_haploid == 3  # three non-PAR male haploids (two slivers + core)
    assert result.nonpar_has_haploid is True  # type: ignore[attr-defined]
    # has_haploid_gt is the existence-only short-circuit the prep assertions now use:
    # True on the non-PAR subset (male haploids present), False on the haploid-free PARs.
    assert has_haploid_gt(panel.chrx_nonpar_panel, bgzip_bin=_BGZIP, awk_bin=_AWK) is True
    assert has_haploid_gt(panel.chrx_par1_panel, bgzip_bin=_BGZIP, awk_bin=_AWK) is False
    assert has_haploid_gt(panel.chrx_par2_panel, bgzip_bin=_BGZIP, awk_bin=_AWK) is False


def test_region_split_ensures_panel_index(tmp_path: Path) -> None:
    panel = _panel_with_chrx(tmp_path / "panel", _records_vcf(_REALISTIC_ROWS))
    chrx = panel.panel_for_chrom("X")
    assert chrx is not None
    tbi = chrx.with_name(chrx.name + ".tbi")
    assert not tbi.is_file()  # install_panel does not fetch one

    _split(panel)

    assert tbi.is_file()  # prepare ensured it for `bcftools view -r`


def test_region_split_is_idempotent(tmp_path: Path) -> None:
    panel = _panel_with_chrx(tmp_path / "panel", _records_vcf(_REALISTIC_ROWS))
    _split(panel)

    again = _split(panel)
    assert again.skipped is True  # type: ignore[attr-defined]


def test_region_split_missing_input_raises(tmp_path: Path) -> None:
    panel = ReferencePanel.resolve(tmp_path / "panel")
    panel.ensure_layout()  # no chrX panel written
    with pytest.raises(ChrxToolingError, match="chrX reference panel VCF is missing"):
        _split(panel)


def test_region_split_rejects_haploid_in_par(tmp_path: Path) -> None:
    # A male haploid at a PAR1 position is biologically impossible — the split is
    # wrong and prep must refuse rather than hand Beagle a mixed-ploidy PAR subset.
    rows = [(1_000_000, "1", "0|0"), (50_000_000, "0", "1|1")]
    panel = _panel_with_chrx(tmp_path / "panel", _records_vcf(rows))
    with pytest.raises(ChrxToolingError, match="par1 subset has"):
        _split(panel)
    # Bad subsets are cleaned up, not left for Beagle.
    assert not panel.chrx_par1_panel.is_file()


def test_region_split_rejects_nonpar_without_haploid(tmp_path: Path) -> None:
    # non-PAR with no male haploid means the split routed males wrong — refuse.
    rows = [(1_000_000, "0|1", "0|0"), (50_000_000, "0|0", "1|1")]
    panel = _panel_with_chrx(tmp_path / "panel", _records_vcf(rows))
    with pytest.raises(ChrxToolingError, match="non-PAR subset has zero haploid"):
        _split(panel)


# ---------------------------------------------------------------------------
# rediploidize_vcf — the R1 seam (un-gated, idempotent).
# ---------------------------------------------------------------------------


def test_rediploidize_doubles_haploid_only(tmp_path: Path) -> None:
    rows = [
        (50_000_000, "1", "0|1"),  # male haploid -> 1|1; female diploid untouched
        (50_000_001, "0", "1|1"),  # male haploid -> 0|0
        (1_000_000, "0|1", "0|0"),  # both diploid -> untouched
    ]
    src = tmp_path / "in.vcf.gz"
    out = tmp_path / "out.vcf.gz"
    _bgzip_vcf(_records_vcf(rows), src)

    doubled = rediploidize_vcf(src, out, bgzip_bin=_BGZIP, awk_bin=_AWK)

    assert doubled == 2  # two male haploids doubled
    assert count_haploid_gts(out, bgzip_bin=_BGZIP, awk_bin=_AWK) == 0
    got = {pos: (m, f) for pos, m, f in _data_records(_bgunzip(out))}
    assert got[50_000_000] == ("1|1", "0|1")
    assert got[50_000_001] == ("0|0", "1|1")
    assert got[1_000_000] == ("0|1", "0|0")


def test_rediploidize_is_ungated_doubles_at_par_positions(tmp_path: Path) -> None:
    # Unlike the deleted M1 diploidizer (which gated on non-PAR position), the R1
    # seam doubles a haploid regardless of position — it runs only on the already
    # region-split non-PAR output, so position no longer matters.
    src = tmp_path / "in.vcf.gz"
    out = tmp_path / "out.vcf.gz"
    _bgzip_vcf(_records_vcf([(1_000_000, "1", "0|0")]), src)  # PAR1 position
    rediploidize_vcf(src, out, bgzip_bin=_BGZIP, awk_bin=_AWK)
    got = {pos: (m, f) for pos, m, f in _data_records(_bgunzip(out))}
    assert got[1_000_000] == ("1|1", "0|0")


def test_rediploidize_preserves_extra_format_subfields(tmp_path: Path) -> None:
    """Only the GT subfield is doubled; trailing FORMAT subfields (DS) survive."""
    text = (
        "##fileformat=VCFv4.2\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        '##FORMAT=<ID=DS,Number=1,Type=Float,Description="Dosage">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tMALE\tFEMALE\n"
        "chrX\t50000000\t.\tA\tG\t.\tPASS\t.\tGT:DS\t0:0.9\t0|1:1.2\n"
    )
    src = tmp_path / "in.vcf.gz"
    out = tmp_path / "out.vcf.gz"
    _bgzip_vcf(text, src)
    rediploidize_vcf(src, out, bgzip_bin=_BGZIP, awk_bin=_AWK)
    pos, male, female = _data_records(_bgunzip(out))[0]
    assert (pos, male, female) == (50000000, "0|0:0.9", "0|1:1.2")


def test_rediploidize_doubles_haploid_no_call(tmp_path: Path) -> None:
    src = tmp_path / "in.vcf.gz"
    out = tmp_path / "out.vcf.gz"
    _bgzip_vcf(_records_vcf([(50_000_000, ".", "0|1")]), src)
    rediploidize_vcf(src, out, bgzip_bin=_BGZIP, awk_bin=_AWK)
    got = {pos: (m, f) for pos, m, f in _data_records(_bgunzip(out))}
    assert got[50_000_000] == (".|.", "0|1")


def test_rediploidize_is_idempotent_on_diploid(tmp_path: Path) -> None:
    # The female / PAR no-op path: already-diploid input is left verbatim.
    src = tmp_path / "in.vcf.gz"
    out = tmp_path / "out.vcf.gz"
    _bgzip_vcf(_records_vcf([(50_000_000, "0|0", "0|1"), (50_000_001, "1|1", "1|0")]), src)
    doubled = rediploidize_vcf(src, out, bgzip_bin=_BGZIP, awk_bin=_AWK)
    assert doubled == 0  # nothing to double
    assert count_haploid_gts(out, bgzip_bin=_BGZIP, awk_bin=_AWK) == 0
    got = {pos: (m, f) for pos, m, f in _data_records(_bgunzip(out))}
    assert got[50_000_000] == ("0|0", "0|1")
    assert got[50_000_001] == ("1|1", "1|0")


# ---------------------------------------------------------------------------
# CLI pre-flight: a chrX run requires the three region panel subsets.
# ---------------------------------------------------------------------------


@pytest.fixture
def panel_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_settings: dict[str, str],  # noqa: ARG001 — sets env + cached settings
) -> Iterator[Path]:
    root = tmp_path / "panel-root"
    monkeypatch.setenv("IMPUTATION_PANEL_ROOT", str(root))
    from genome.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    try:
        yield root
    finally:
        get_settings.cache_clear()


def test_require_chrx_region_panels_aborts_when_missing(
    panel_root: Path,  # noqa: ARG001 — fixture redirects the panel root
) -> None:
    import typer  # noqa: PLC0415

    with pytest.raises(typer.Exit):
        _require_chrx_region_panels(frozenset({"X"}))
    with pytest.raises(typer.Exit):
        _require_chrx_region_panels(None)  # full run includes X


def test_require_chrx_region_panels_noop_for_autosomes(
    panel_root: Path,  # noqa: ARG001 — fixture redirects the panel root
) -> None:
    _require_chrx_region_panels(frozenset({"1", "2"}))  # no raise


def test_require_chrx_region_panels_requires_all_three(
    panel_root: Path,  # noqa: ARG001 — fixture redirects the panel root
) -> None:
    import typer  # noqa: PLC0415

    panel = ReferencePanel.resolve()
    panel.ensure_layout()
    # Only two of the three present -> still aborts.
    panel.chrx_par1_panel.write_bytes(b"x")
    panel.chrx_nonpar_panel.write_bytes(b"x")
    with pytest.raises(typer.Exit):
        _require_chrx_region_panels(frozenset({"X"}))

    panel.chrx_par2_panel.write_bytes(b"x")
    _require_chrx_region_panels(frozenset({"X"}))  # all three present -> no raise
