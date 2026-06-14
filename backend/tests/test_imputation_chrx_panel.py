"""Tests for the M1 chrX panel diploidizer (PR 5a, finding-008/029).

These exercise the *real* ``bgzip``/``awk`` transform on small synthetic BGZF
chrX VCFs (skipped when either tool is absent), so the awk program that runs in
production is what is under test. They pin: non-PAR male haploid → homozygous
diploid, PAR + female untouched, the GT:DS subfield rule, the full-chromosome
zero-haploid assertion, and the awk's non-PAR gating == :func:`is_nonpar`.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from genome.cli import _require_diploidized_chrx_panel
from genome.imputation.chrx_panel import (
    ChrxToolingError,
    count_haploid_gts,
    diploidize_chrx_panel,
    prepare_chrx_panel,
)
from genome.imputation.reference_panel import ReferencePanel
from genome.par_regions import is_nonpar

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    shutil.which("bgzip") is None or shutil.which("awk") is None,
    reason="bgzip and awk are required for the chrX diploidizer",
)

_BGZIP = shutil.which("bgzip") or "bgzip"
_AWK = shutil.which("awk") or "awk"

_VCF_HEADER = (
    "##fileformat=VCFv4.2\n"
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


def test_diploidize_rewrites_non_par_male_haploid_only(tmp_path: Path) -> None:
    rows = [
        (1_000_000, "0|1", "0|0"),  # PAR1: already diploid — untouched
        (5_000, "1", "0|1"),  # sliver below PAR1 (non-PAR) — male haploid -> 1|1
        (50_000_000, "1", "0|1"),  # non-PAR core — male haploid -> 1|1
        (50_000_001, "0", "1|1"),  # non-PAR core — male haploid -> 0|0
        (155_800_000, "0|1", "0|0"),  # PAR2: males are diploid here — untouched
    ]
    src = tmp_path / "chrX.vcf.gz"
    out = tmp_path / "chrX.diploidized.vcf.gz"
    _bgzip_vcf(_records_vcf(rows), src)

    # Three non-PAR male haploids in the input; none after the transform.
    assert count_haploid_gts(src, bgzip_bin=_BGZIP, awk_bin=_AWK) == 3
    diploidize_chrx_panel(src, out, bgzip_bin=_BGZIP, awk_bin=_AWK)
    assert count_haploid_gts(out, bgzip_bin=_BGZIP, awk_bin=_AWK) == 0

    got = {pos: (male, female) for pos, male, female in _data_records(_bgunzip(out))}
    assert got[1_000_000] == ("0|1", "0|0")  # PAR1 untouched
    assert got[5_000] == ("1|1", "0|1")  # sliver male diploidized, female kept
    assert got[50_000_000] == ("1|1", "0|1")
    assert got[50_000_001] == ("0|0", "1|1")
    assert got[155_800_000] == ("0|1", "0|0")  # PAR2 untouched


def test_diploidize_preserves_extra_format_subfields(tmp_path: Path) -> None:
    """Only the GT subfield is doubled; trailing FORMAT subfields (DS) survive."""
    text = (
        "##fileformat=VCFv4.2\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        '##FORMAT=<ID=DS,Number=1,Type=Float,Description="Dosage">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tMALE\tFEMALE\n"
        "chrX\t50000000\t.\tA\tG\t.\tPASS\t.\tGT:DS\t0:0.9\t0|1:1.2\n"
    )
    src = tmp_path / "chrX.vcf.gz"
    out = tmp_path / "chrX.diploidized.vcf.gz"
    _bgzip_vcf(text, src)
    diploidize_chrx_panel(src, out, bgzip_bin=_BGZIP, awk_bin=_AWK)
    pos, male, female = _data_records(_bgunzip(out))[0]
    assert (pos, male, female) == (50000000, "0|0:0.9", "0|1:1.2")


def test_awk_non_par_gating_matches_is_nonpar(tmp_path: Path) -> None:
    """A male haploid is diploidized exactly at the positions ``is_nonpar`` is True."""
    positions = [
        1,
        10_000,
        10_001,
        2_781_479,
        2_781_480,
        50_000_000,
        155_701_383,
        156_030_895,
        156_030_896,
    ]
    rows = [(pos, "1", "0|1") for pos in positions]
    src = tmp_path / "chrX.vcf.gz"
    out = tmp_path / "chrX.diploidized.vcf.gz"
    _bgzip_vcf(_records_vcf(rows), src)
    diploidize_chrx_panel(src, out, bgzip_bin=_BGZIP, awk_bin=_AWK)

    for pos, male, _female in _data_records(_bgunzip(out)):
        expected = "1|1" if is_nonpar(pos) else "1"
        assert male == expected, f"pos {pos}: male={male}, is_nonpar={is_nonpar(pos)}"


# ---------------------------------------------------------------------------
# prepare_chrx_panel orchestration.
# ---------------------------------------------------------------------------


def _panel_with_chrx(root: Path, vcf_text: str) -> ReferencePanel:
    panel = ReferencePanel.resolve(root)
    panel.ensure_layout()
    chrx = panel.panel_for_chrom("X")
    assert chrx is not None
    _bgzip_vcf(vcf_text, chrx)
    return panel


def test_prepare_chrx_panel_produces_and_verifies_output(tmp_path: Path) -> None:
    rows = [(50_000_000, "1", "0|1"), (50_000_001, "0", "1|1"), (1_000_000, "0|1", "0|0")]
    panel = _panel_with_chrx(tmp_path / "panel", _records_vcf(rows))

    result = prepare_chrx_panel(panel, bgzip_bin=_BGZIP, awk_bin=_AWK)

    assert result.skipped is False
    assert result.diploidized_gts == 2  # two non-PAR male haploids
    assert result.haploid_remaining == 0
    assert result.output_path == panel.diploidized_chrx_panel
    assert panel.diploidized_chrx_panel.is_file()
    assert count_haploid_gts(panel.diploidized_chrx_panel, bgzip_bin=_BGZIP, awk_bin=_AWK) == 0


def test_prepare_chrx_panel_is_idempotent(tmp_path: Path) -> None:
    rows = [(50_000_000, "1", "0|1")]
    panel = _panel_with_chrx(tmp_path / "panel", _records_vcf(rows))
    prepare_chrx_panel(panel, bgzip_bin=_BGZIP, awk_bin=_AWK)

    again = prepare_chrx_panel(panel, bgzip_bin=_BGZIP, awk_bin=_AWK)
    assert again.skipped is True


def test_prepare_chrx_panel_missing_input_raises(tmp_path: Path) -> None:
    panel = ReferencePanel.resolve(tmp_path / "panel")
    panel.ensure_layout()  # no chrX panel written
    with pytest.raises(ChrxToolingError, match="chrX reference panel VCF is missing"):
        prepare_chrx_panel(panel, bgzip_bin=_BGZIP, awk_bin=_AWK)


# ---------------------------------------------------------------------------
# CLI pre-flight: chrX run requires the diploidized panel.
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


def test_require_diploidized_panel_aborts_when_missing(
    panel_root: Path,  # noqa: ARG001 — fixture redirects the panel root
) -> None:
    import typer  # noqa: PLC0415

    with pytest.raises(typer.Exit):
        _require_diploidized_chrx_panel(frozenset({"X"}))
    with pytest.raises(typer.Exit):
        _require_diploidized_chrx_panel(None)  # full run includes X


def test_require_diploidized_panel_noop_for_autosomes(
    panel_root: Path,  # noqa: ARG001 — fixture redirects the panel root
) -> None:
    _require_diploidized_chrx_panel(frozenset({"1", "2"}))  # no raise


def test_require_diploidized_panel_passes_when_present(
    panel_root: Path,  # noqa: ARG001 — fixture redirects the panel root
) -> None:
    panel = ReferencePanel.resolve()
    panel.ensure_layout()
    panel.diploidized_chrx_panel.write_bytes(b"x")
    _require_diploidized_chrx_panel(frozenset({"X"}))  # no raise
