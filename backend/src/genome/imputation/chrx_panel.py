"""M1 chrX reference-panel diploidizer (PR 5a, finding-008/029).

Beagle 5.5's reference loader rejects the 1000 Genomes panel's *within-sample*
ploidy transition: a male sample is diploid through PAR1 and haploid past it, and
Beagle aborts at the first non-PAR position (finding-008 #2). The M1 mechanic,
decided after the planning probe, rewrites male non-PAR haploid genotypes to
homozygous-diploid so the whole panel is uniform-diploid and Beagle accepts it.
PAR positions (already diploid in both sexes) are left untouched.

The transform is the probe-validated ``bgzip | awk | bgzip`` stream — awk does
the per-field genotype rewrite at C speed (a pure-Python pass over a 3202-sample
chrX panel would be far too slow), gated to non-PAR positions by the same PAR1 /
PAR2 boundaries as :mod:`genome.par_regions`. It is **not** ``bcftools
+fixploidy``, which the probe did not byte-validate. After the rewrite the whole
chromosome is asserted haploid-free before any Beagle run.

Output lands beside the panel as ``chrX.diploidized.vcf.gz``; the runner points
its chrX ``ref=`` at this file.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from genome.imputation.archive import restrict_file

if TYPE_CHECKING:
    from pathlib import Path

    from genome.imputation.reference_panel import ReferencePanel

logger = structlog.get_logger(__name__)

# Non-PAR gating uses the same GRCh38 PAR1 / PAR2 boundaries as
# genome.par_regions (kept as awk literals; a parity test pins the agreement).
# For each non-PAR data record, every sample field whose GT subfield (the first
# ':'-delimited token) carries no phase/unphase separator is haploid — doubled
# into a phased homozygote. Header lines and PAR records pass through verbatim.
_DIPLOIDIZE_AWK: Final[str] = r"""
BEGIN { OFS = "\t" }
/^#/ { print; next }
{
  pos = $2 + 0
  if (!((pos >= 10001 && pos <= 2781479) || (pos >= 155701383 && pos <= 156030895))) {
    for (i = 10; i <= NF; i++) {
      n = split($i, a, ":")
      if (a[1] !~ /[|\/]/) {
        $i = a[1] "|" a[1]
        for (j = 2; j <= n; j++) $i = $i ":" a[j]
      }
    }
  }
  print
}
"""

# Counts haploid GT fields across every (non-header) record — used to size the
# work before the transform and to assert zero remain after it.
_COUNT_HAPLOID_AWK: Final[str] = r"""
/^#/ { next }
{ for (i = 10; i <= NF; i++) { split($i, a, ":"); if (a[1] !~ /[|\/]/) h++ } }
END { print h + 0 }
"""


@dataclass(frozen=True, slots=True)
class ChrxPanelResult:
    """Summary returned by :func:`prepare_chrx_panel`."""

    output_path: Path
    skipped: bool
    diploidized_gts: int
    haploid_remaining: int


class ChrxToolingError(RuntimeError):
    """``bgzip`` / ``awk`` is unavailable, or a transform subprocess failed."""


def _resolve_tools(bgzip_bin: str | None, awk_bin: str | None) -> tuple[str, str]:
    bgzip = bgzip_bin or shutil.which("bgzip")
    awk = awk_bin or shutil.which("awk")
    if bgzip is None or awk is None:
        missing = [name for name, found in (("bgzip", bgzip), ("awk", awk)) if found is None]
        msg = (
            f"chrX panel diploidization needs {', '.join(missing)} on PATH. "
            "Install htslib (bgzip) and a POSIX awk and re-run."
        )
        raise ChrxToolingError(msg)
    return bgzip, awk


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


def diploidize_chrx_panel(
    input_vcf: Path, output_vcf: Path, *, bgzip_bin: str, awk_bin: str
) -> None:
    """Stream ``input_vcf`` through the diploidizer into a fresh BGZF ``output_vcf``.

    ``bgzip -dc input | awk <transform> | bgzip -c > output`` — fully streamed,
    so no multi-GB uncompressed intermediate ever lands on disk. A partial output
    is removed on failure.
    """
    with output_vcf.open("wb") as out_fh:
        p_decomp = subprocess.Popen(  # noqa: S603
            [bgzip_bin, "-dc", str(input_vcf)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert p_decomp.stdout is not None  # noqa: S101
        p_awk = subprocess.Popen(  # noqa: S603
            [awk_bin, _DIPLOIDIZE_AWK],
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
            f"chrX diploidize pipe failed (bgzip-d rc={rc_decomp}, awk rc={rc_awk}, "
            f"bgzip-c rc={rc_comp})"
        )
        raise ChrxToolingError(msg)


def prepare_chrx_panel(
    panel: ReferencePanel,
    *,
    force: bool = False,
    bgzip_bin: str | None = None,
    awk_bin: str | None = None,
) -> ChrxPanelResult:
    """Produce ``chrX.diploidized.vcf.gz`` from the installed chrX panel (M1).

    Idempotent: an existing output is reused unless ``force`` is set. After the
    transform the entire chromosome is asserted haploid-free (the §7 full-
    chromosome boundary check); a non-zero residual raises and the bad output is
    removed, so a partial transform can never be handed to Beagle.
    """
    bgzip, awk = _resolve_tools(bgzip_bin, awk_bin)
    input_vcf = panel.panel_for_chrom("X")
    if input_vcf is None or not input_vcf.is_file():
        msg = (
            "chrX reference panel VCF is missing; run `genome imputation panel install` "
            "before `panel prepare-chrx`."
        )
        raise ChrxToolingError(msg)

    output_vcf = panel.diploidized_chrx_panel
    log = logger.bind(input=str(input_vcf), output=str(output_vcf))
    if output_vcf.is_file() and not force:
        log.info("imputation.panel.prepare_chrx.skip_existing")
        return ChrxPanelResult(
            output_path=output_vcf,
            skipped=True,
            diploidized_gts=0,
            haploid_remaining=0,
        )

    to_diploidize = count_haploid_gts(input_vcf, bgzip_bin=bgzip, awk_bin=awk)
    log.info("imputation.panel.prepare_chrx.start", haploid_gts=to_diploidize)
    diploidize_chrx_panel(input_vcf, output_vcf, bgzip_bin=bgzip, awk_bin=awk)

    remaining = count_haploid_gts(output_vcf, bgzip_bin=bgzip, awk_bin=awk)
    if remaining != 0:
        output_vcf.unlink(missing_ok=True)
        msg = (
            f"diploidized chrX panel still has {remaining} haploid GT field(s) — the "
            "transform missed a non-PAR boundary; refusing to hand a mixed-ploidy panel "
            "to Beagle."
        )
        raise ChrxToolingError(msg)

    restrict_file(output_vcf)
    log.info(
        "imputation.panel.prepare_chrx.complete",
        diploidized_gts=to_diploidize,
        haploid_remaining=0,
    )
    return ChrxPanelResult(
        output_path=output_vcf,
        skipped=False,
        diploidized_gts=to_diploidize,
        haploid_remaining=0,
    )


__all__ = [
    "ChrxPanelResult",
    "ChrxToolingError",
    "count_haploid_gts",
    "diploidize_chrx_panel",
    "prepare_chrx_panel",
]
