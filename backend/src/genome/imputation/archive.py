"""Filesystem layout for an imputation roundtrip.

Each roundtrip gets its own subdirectory under ``<archive_root>/imputation/``:

::

    <archive_root>/imputation/
        run_0001/
            upload/                  # per-chromosome VCFs ready for imputation
                chr1.vcf.gz
                chr2.vcf.gz
                ...
                MANIFEST.json        # what we exported and how
            result/                  # imputed per-chromosome VCFs
                chr1.vcf.gz          # Beagle output
                ...
                (legacy TopMed runs may have chr*.dose.vcf.gz instead)

``run_0001`` matches ``imputation_runs.imputation_id`` zero-padded to 4 digits;
this makes ``ls`` output sort correctly and keeps the on-disk layout aligned
with the database.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from pathlib import Path

_OWNER_RW_ONLY = stat.S_IRUSR | stat.S_IWUSR
_OWNER_RWX_ONLY = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR

ChrxRegion = Literal["par1", "nonpar", "par2"]
"""The three physical chrX regions M3-physical imputes independently (PR 5a)."""

CHRX_REGIONS: Final[tuple[ChrxRegion, ...]] = ("par1", "nonpar", "par2")
"""Canonical chrX region order. ``bcftools concat -a`` re-sorts by coordinate, so
this order is for deterministic iteration, not the final record order."""


@dataclass(frozen=True, slots=True)
class ImputationArchive:
    """Directory tree for a single imputation run."""

    root: Path
    """Per-run root: ``<archive_root>/imputation/run_<id>/``."""

    @classmethod
    def for_run(cls, archive_root: Path, imputation_id: int) -> ImputationArchive:
        """Build an archive object for ``imputation_id`` rooted at ``archive_root``.

        The directories are created lazily by :meth:`ensure_layout` so this
        constructor is a pure value object. That makes it cheap to instantiate
        in tests without side effects.
        """
        return cls(root=archive_root / "imputation" / f"run_{imputation_id:04d}")

    @property
    def upload_dir(self) -> Path:
        """``<root>/upload/`` — per-chromosome VCFs the prepare step emits for Beagle.

        Named ``upload/`` for historical reasons (Phase 4 originally
        uploaded these to TopMed; the local Beagle workflow that replaced
        it consumes them in place from this same directory).
        """
        return self.root / "upload"

    @property
    def result_dir(self) -> Path:
        """``<root>/result/`` — Beagle's imputed per-chromosome VCFs land here."""
        return self.root / "result"

    @property
    def chrx_region_upload_dir(self) -> Path:
        """``<upload_dir>/chrX_regions/`` — M3-physical chrX target subsets (PR 5a).

        One level below the top-level ``chr*.vcf.gz`` upload glob, so the runner's
        :func:`list_upload_vcfs` never sees these region files.
        """
        return self.upload_dir / "chrX_regions"

    @property
    def chrx_region_result_dir(self) -> Path:
        """``<result_dir>/chrX_regions/`` — M3-physical per-region Beagle output (PR 5a).

        One level below the top-level ``chr*.vcf.gz`` result glob, so the importer's
        :func:`list_result_vcfs` sees only the concat ``result/chrX.vcf.gz``.
        """
        return self.result_dir / "chrX_regions"

    @property
    def upload_manifest(self) -> Path:
        """``<upload_dir>/MANIFEST.json`` — what the prepare step produced."""
        return self.upload_dir / "MANIFEST.json"

    @property
    def chrx_loo_dir(self) -> Path:
        """``<root>/loo/`` — scratch for the chrX non-PAR leave-one-out harness (PR 5a).

        Per-fold masked targets, Beagle outputs, and the JSON report land here.
        It sits under the per-run archive (on the big disk), deliberately **never**
        ``/tmp`` — the chrX LOO long-op streams multi-GB Beagle scratch and the
        host's ``/tmp`` is a small, near-full tmpfs (finding-031 / PR 5a plan).
        """
        return self.root / "loo"

    @property
    def encrypted_archive(self) -> Path:
        """``<result_dir>/topmed_result.zip`` — legacy TopMed encrypted archive path."""
        return self.result_dir / "topmed_result.zip"

    @property
    def download_metadata(self) -> Path:
        """``<result_dir>/topmed_result.sha256`` — legacy TopMed checksum path."""
        return self.result_dir / "topmed_result.sha256"

    def ensure_layout(self) -> None:
        """Create the per-run directory tree with restrictive permissions.

        Idempotent: existing directories are left alone but their permissions
        are forced to ``0700`` so a previously-permissive parent gets tightened.
        The two ``chrX_regions/`` subdirectories are created here too so the
        M3-physical prepare / run steps can write into them without their own
        ``mkdir`` (PR 5a).
        """
        for d in (
            self.root,
            self.upload_dir,
            self.result_dir,
            self.chrx_region_upload_dir,
            self.chrx_region_result_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(_OWNER_RWX_ONLY)

    def upload_vcf_path(self, chrom: str) -> Path:
        """Per-chromosome VCF path. ``chrom`` is the canonical label ('1'..'22','X','Y','MT')."""
        return self.upload_dir / f"chr{chrom}.vcf.gz"

    def chrx_region_upload_path(self, region: ChrxRegion) -> Path:
        """M3-physical chrX target subset path (``upload/chrX_regions/<region>.vcf.gz``)."""
        return self.chrx_region_upload_dir / f"{region}.vcf.gz"

    def chrx_region_result_path(self, region: ChrxRegion) -> Path:
        """M3-physical per-region Beagle output path (``result/chrX_regions/<region>.vcf.gz``)."""
        return self.chrx_region_result_dir / f"{region}.vcf.gz"

    def chrx_region_result_diploid_path(self) -> Path:
        """Non-PAR output after the R1 re-diploidize seam.

        Lands at ``result/chrX_regions/nonpar.diploid.vcf.gz``. This is what feeds
        ``bcftools concat`` for the non-PAR slot, so the
        concat output is uniform-diploid even when Beagle emitted haploid male
        non-PAR calls (PR 5a, finding-029 R1).
        """
        return self.chrx_region_result_dir / "nonpar.diploid.vcf.gz"

    def list_upload_vcfs(self) -> list[Path]:
        """All ``chr*.vcf.gz`` files in the upload directory, sorted alphabetically."""
        if not self.upload_dir.is_dir():
            return []
        return sorted(self.upload_dir.glob("chr*.vcf.gz"))

    def list_result_vcfs(self) -> list[Path]:
        """All ``chr*.vcf.gz`` files in the result directory.

        Matches both the Beagle runner's output (``chr<N>.vcf.gz``) and any
        legacy TopMed result files (``chr<N>.dose.vcf.gz`` — also a glob
        match of ``chr*.vcf.gz``). Non-VCF files in the result directory
        (e.g. encrypted-archive bookkeeping) are filtered out by the glob.
        """
        if not self.result_dir.is_dir():
            return []
        return sorted(self.result_dir.glob("chr*.vcf.gz"))


def restrict_file(path: Path) -> None:
    """Chmod ``path`` to ``0600``. Used for any file containing genome data."""
    if path.exists():
        path.chmod(_OWNER_RW_ONLY)
