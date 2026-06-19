"""M3-physical chrX panel region split + R1 re-diploidization seam (PR 5a).

Beagle 5.5's reference loader rejects the 1000 Genomes chrX panel's *within-sample*
ploidy transition — a male sample is diploid through PAR1 and haploid past it, and
Beagle aborts at the first non-PAR position (finding-008 #2). The shipped M1
mechanic worked around this by diploidizing the *entire* panel (male non-PAR
haploid → fake hom-diploid). Its first authoritative run failed M1's own
falsifiability gate: whole-panel diploidization destroys non-PAR information
content, yielding mean DR² ≈ 0 (finding-029).

**M3-physical** is the fix. Instead of mutating ploidy, split the panel into three
**physical** region subsets — PAR1 / non-PAR / PAR2 — via ``bcftools view -r``.
Each subset is internally uniform (no within-sample transition), so Beagle loads
it natively: the non-PAR subset keeps male haplotypes *haploid*, the
biologically-correct field-standard representation, and imputes faithfully. The
runner (:mod:`genome.imputation.beagle_runner`) imputes each region against its
matching native subset, then ``bcftools concat -a`` re-joins them.

**R1 storage** keeps the rest of the pipeline byte-unchanged: the male non-PAR
Beagle output is haploid, so :func:`rediploidize_vcf` doubles it back to
hom-diploid (``0`` → ``0|0``) before the importer ever sees it. The seam is
idempotent — a no-op on already-diploid (female / PAR) output — so the runner runs
it unconditionally on the non-PAR slot and never needs to know the sample's sex.

This module owns the panel-side split (:func:`prepare_chrx_panel`) and the
re-diploidizer (:func:`rediploidize_vcf`). The region boundaries are derived from
:mod:`genome.par_regions` so the coordinate literals live in exactly one place.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from genome.imputation.archive import restrict_file
from genome.par_regions import PAR1_END, PAR1_START, PAR2_END, PAR2_START

if TYPE_CHECKING:
    from pathlib import Path

    from genome.imputation.reference_panel import ReferencePanel

logger = structlog.get_logger(__name__)

# The panel contig label. The 1000 Genomes high-coverage chrX panel uses the
# single-``chr``-prefixed ``chrX`` (verified against the installed panel); Beagle
# matches contig labels by exact string, and the region strings below must agree.
_CHRX_CONTIG: Final[str] = "chrX"

# Counts haploid GT fields across every (non-header) record — used at prep time to
# assert the PAR subsets are haploid-free and the non-PAR subset retains males, and
# as the re-diploidizer's haploid-free post-assertion.
_COUNT_HAPLOID_AWK: Final[str] = r"""
/^#/ { next }
{ for (i = 10; i <= NF; i++) { split($i, a, ":"); if (a[1] !~ /[|\/]/) h++ } }
END { print h + 0 }
"""

# R1 re-diploidizer (un-gated; runs on EVERY record, unlike the deleted M1
# diploidizer which gated on non-PAR position). For each sample field, if the GT
# subfield carries no phase/unphase separator it is haploid — doubled into a
# phased homozygote, preserving trailing ':'-delimited FORMAT subfields (DS).
# Idempotent: an already-diploid GT is left verbatim.
_REDIPLOIDIZE_AWK: Final[str] = r"""
BEGIN { OFS = "\t" }
/^#/ { print; next }
{
  for (i = 10; i <= NF; i++) {
    n = split($i, a, ":")
    if (a[1] !~ /[|\/]/) {
      $i = a[1] "|" a[1]
      for (j = 2; j <= n; j++) $i = $i ":" a[j]
    }
  }
  print
}
"""


@dataclass(frozen=True, slots=True)
class ChrxPanelResult:
    """Summary returned by :func:`prepare_chrx_panel` — the three native subsets."""

    par1_path: Path
    nonpar_path: Path
    par2_path: Path
    skipped: bool
    nonpar_haploid_gts: int


class ChrxToolingError(RuntimeError):
    """``bcftools`` / ``bgzip`` / ``awk`` is unavailable, or a subprocess failed."""


def _resolve_tools(bgzip_bin: str | None, awk_bin: str | None) -> tuple[str, str]:
    bgzip = bgzip_bin or shutil.which("bgzip")
    awk = awk_bin or shutil.which("awk")
    if bgzip is None or awk is None:
        missing = [name for name, found in (("bgzip", bgzip), ("awk", awk)) if found is None]
        msg = (
            f"chrX panel prep needs {', '.join(missing)} on PATH. "
            "Install htslib (bgzip) and a POSIX awk and re-run."
        )
        raise ChrxToolingError(msg)
    return bgzip, awk


def _resolve_bcftools(bcftools_bin: str | None) -> str:
    bcftools = bcftools_bin or shutil.which("bcftools")
    if bcftools is None:
        msg = (
            "chrX panel region split needs bcftools on PATH. Install bcftools (htslib) and re-run."
        )
        raise ChrxToolingError(msg)
    return bcftools


def count_haploid_gts(vcf: Path, *, bgzip_bin: str, awk_bin: str) -> int:
    """Return the number of haploid GT fields across all records in ``vcf``."""
    p_decomp = subprocess.Popen(  # noqa: S603 — bins are resolved/validated paths
        [bgzip_bin, "-dc", str(vcf)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert p_decomp.stdout is not None  # noqa: S101 — stdout=PIPE guarantees non-None
    p_awk = subprocess.Popen(  # noqa: S603
        [awk_bin, _COUNT_HAPLOID_AWK],
        stdin=p_decomp.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    p_decomp.stdout.close()
    out, _ = p_awk.communicate()
    rc_decomp = p_decomp.wait()
    if p_awk.returncode != 0 or rc_decomp != 0:
        msg = (
            f"haploid-count pipe failed (bgzip rc={rc_decomp}, awk rc={p_awk.returncode}) on {vcf}"
        )
        raise ChrxToolingError(msg)
    return int(out.strip() or "0")


def _par1_region() -> str:
    """``bcftools -r`` region string for PAR1."""
    return f"{_CHRX_CONTIG}:{PAR1_START}-{PAR1_END}"


def _par2_region() -> str:
    """``bcftools -r`` region string for PAR2."""
    return f"{_CHRX_CONTIG}:{PAR2_START}-{PAR2_END}"


def _nonpar_region() -> str:
    """``bcftools -r`` region string for the non-PAR complement of PAR1 and PAR2.

    Three comma-joined ranges: the lower telomeric sliver below PAR1, the core
    between the PARs, and the open-ended upper range above PAR2 (no end bound, so
    it reaches the contig end regardless of assembly length). Boundaries are
    derived from :mod:`genome.par_regions` so the literals live in one place.
    """
    return (
        f"{_CHRX_CONTIG}:1-{PAR1_START - 1},"
        f"{_CHRX_CONTIG}:{PAR1_END + 1}-{PAR2_START - 1},"
        f"{_CHRX_CONTIG}:{PAR2_END + 1}-"
    )


def _ensure_panel_index(panel_vcf: Path, bcftools_bin: str) -> None:
    """Build the panel's ``.tbi`` if missing or older than the VCF.

    ``bcftools view -r`` needs a coordinate index; ``install_panel`` does not
    fetch one. Idempotent: a present, up-to-date ``.tbi`` is left alone.
    """
    tbi = panel_vcf.with_name(panel_vcf.name + ".tbi")
    if tbi.is_file() and tbi.stat().st_mtime >= panel_vcf.stat().st_mtime:
        return
    proc = subprocess.run(  # noqa: S603 — bin is a resolved/validated path
        [bcftools_bin, "index", "-t", "-f", str(panel_vcf)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = f"bcftools index of {panel_vcf} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        raise ChrxToolingError(msg)


def _bcftools_view_region(
    bcftools_bin: str, input_vcf: Path, region: str, output_vcf: Path
) -> None:
    """Extract ``region`` from ``input_vcf`` into a fresh BGZF ``output_vcf``."""
    proc = subprocess.run(  # noqa: S603 — bin is a resolved/validated path
        [bcftools_bin, "view", "-r", region, str(input_vcf), "-Oz", "-o", str(output_vcf)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        output_vcf.unlink(missing_ok=True)
        msg = (
            f"bcftools view -r {region!r} of {input_vcf} failed "
            f"(rc={proc.returncode}): {proc.stderr.strip()}"
        )
        raise ChrxToolingError(msg)


def _stream_through_awk(
    input_vcf: Path, output_vcf: Path, awk_program: str, *, bgzip_bin: str, awk_bin: str
) -> None:
    """``bgzip -dc input | awk <program> | bgzip -c > output`` — fully streamed.

    No multi-GB uncompressed intermediate ever lands on disk. A partial output is
    removed on failure.
    """
    with output_vcf.open("wb") as out_fh:
        p_decomp = subprocess.Popen(  # noqa: S603
            [bgzip_bin, "-dc", str(input_vcf)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert p_decomp.stdout is not None  # noqa: S101
        p_awk = subprocess.Popen(  # noqa: S603
            [awk_bin, awk_program],
            stdin=p_decomp.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert p_awk.stdout is not None  # noqa: S101
        p_comp = subprocess.Popen(  # noqa: S603
            [bgzip_bin, "-c"],
            stdin=p_awk.stdout,
            stdout=out_fh,
            stderr=subprocess.DEVNULL,
        )
        p_decomp.stdout.close()
        p_awk.stdout.close()
        rc_comp = p_comp.wait()
        rc_awk = p_awk.wait()
        rc_decomp = p_decomp.wait()
    if rc_decomp != 0 or rc_awk != 0 or rc_comp != 0:
        output_vcf.unlink(missing_ok=True)
        msg = (
            f"chrX awk stream failed (bgzip-d rc={rc_decomp}, awk rc={rc_awk}, "
            f"bgzip-c rc={rc_comp})"
        )
        raise ChrxToolingError(msg)


def rediploidize_vcf(input_vcf: Path, output_vcf: Path, *, bgzip_bin: str, awk_bin: str) -> int:
    """Re-diploidize ``input_vcf`` into ``output_vcf`` (R1 seam, PR 5a).

    Doubles any haploid GT subfield (``0`` → ``0|0``, ``1`` → ``1|1``, ``.`` →
    ``.|.``) on every record, preserving trailing ``:``-delimited FORMAT subfields
    (e.g. Beagle's ``DS``). Idempotent: an already-diploid GT is left verbatim, so
    running on diploid (female / PAR) output is a no-op. Asserts the result is
    haploid-free and returns the number of haploid GTs that were doubled (0 on the
    no-op path).
    """
    doubled = count_haploid_gts(input_vcf, bgzip_bin=bgzip_bin, awk_bin=awk_bin)
    _stream_through_awk(
        input_vcf, output_vcf, _REDIPLOIDIZE_AWK, bgzip_bin=bgzip_bin, awk_bin=awk_bin
    )
    remaining = count_haploid_gts(output_vcf, bgzip_bin=bgzip_bin, awk_bin=awk_bin)
    if remaining != 0:
        output_vcf.unlink(missing_ok=True)
        msg = (
            f"re-diploidized {output_vcf} still has {remaining} haploid GT field(s) — "
            "the seam missed a sample; refusing to hand a mixed-ploidy file downstream."
        )
        raise ChrxToolingError(msg)
    return doubled


def _cleanup_subsets(*paths: Path) -> None:
    """Remove partially-written region subsets on a failed prep."""
    for p in paths:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


def prepare_chrx_panel(
    panel: ReferencePanel,
    *,
    force: bool = False,
    bcftools_bin: str | None = None,
    bgzip_bin: str | None = None,
    awk_bin: str | None = None,
) -> ChrxPanelResult:
    """Split the installed chrX panel into native PAR1 / non-PAR / PAR2 subsets (M3).

    Idempotent: existing subsets are reused unless ``force`` is set. Ensures the
    panel ``.tbi`` first (``bcftools view -r`` needs it), then emits the three
    subsets and asserts their composition at prep time: the PAR subsets must be
    haploid-free (PAR is diploid in both sexes) and the non-PAR subset must retain
    haploid male haplotypes (``> 0``). A failed assertion removes the bad subsets
    and raises, so a malformed split can never be handed to Beagle.
    """
    bcftools = _resolve_bcftools(bcftools_bin)
    bgzip, awk = _resolve_tools(bgzip_bin, awk_bin)
    input_vcf = panel.panel_for_chrom("X")
    if input_vcf is None or not input_vcf.is_file():
        msg = (
            "chrX reference panel VCF is missing; run `genome imputation panel install` "
            "before `panel prepare-chrx`."
        )
        raise ChrxToolingError(msg)

    par1_out = panel.chrx_par1_panel
    nonpar_out = panel.chrx_nonpar_panel
    par2_out = panel.chrx_par2_panel
    log = logger.bind(input=str(input_vcf))

    if not force and par1_out.is_file() and nonpar_out.is_file() and par2_out.is_file():
        log.info("imputation.panel.prepare_chrx.skip_existing")
        return ChrxPanelResult(
            par1_path=par1_out,
            nonpar_path=nonpar_out,
            par2_path=par2_out,
            skipped=True,
            nonpar_haploid_gts=0,
        )

    _ensure_panel_index(input_vcf, bcftools)
    log.info("imputation.panel.prepare_chrx.start")
    _bcftools_view_region(bcftools, input_vcf, _par1_region(), par1_out)
    _bcftools_view_region(bcftools, input_vcf, _nonpar_region(), nonpar_out)
    _bcftools_view_region(bcftools, input_vcf, _par2_region(), par2_out)

    for region_name, p in (("par1", par1_out), ("par2", par2_out)):
        haploid = count_haploid_gts(p, bgzip_bin=bgzip, awk_bin=awk)
        if haploid != 0:
            _cleanup_subsets(par1_out, nonpar_out, par2_out)
            msg = (
                f"chrX {region_name} subset has {haploid} haploid GT field(s); PAR is "
                "diploid in both sexes — the region split is wrong, refusing."
            )
            raise ChrxToolingError(msg)

    nonpar_haploid = count_haploid_gts(nonpar_out, bgzip_bin=bgzip, awk_bin=awk)
    if nonpar_haploid == 0:
        _cleanup_subsets(par1_out, nonpar_out, par2_out)
        msg = (
            "chrX non-PAR subset has zero haploid GT fields; the panel's male "
            "hemizygous haplotypes are expected here — the region split is wrong, refusing."
        )
        raise ChrxToolingError(msg)

    for p in (par1_out, nonpar_out, par2_out):
        restrict_file(p)
    log.info("imputation.panel.prepare_chrx.complete", nonpar_haploid_gts=nonpar_haploid)
    return ChrxPanelResult(
        par1_path=par1_out,
        nonpar_path=nonpar_out,
        par2_path=par2_out,
        skipped=False,
        nonpar_haploid_gts=nonpar_haploid,
    )


__all__ = [
    "ChrxPanelResult",
    "ChrxToolingError",
    "count_haploid_gts",
    "prepare_chrx_panel",
    "rediploidize_vcf",
]
