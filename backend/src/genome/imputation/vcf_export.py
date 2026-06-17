"""Export merged genotype calls as per-chromosome GRCh38 VCFs for Beagle 5.5.

We export from ``consensus_genotypes`` joined to ``variants_master`` — the
merged set is the only sensible input (imputing unmerged 23andMe + Ancestry
separately would produce wildly different results on overlapping SNPs).

Format details we honor:

* ``##fileformat=VCFv4.2``.
* ``##contig=<ID=chr1,assembly=GRCh38>`` style declarations with ``chr``-prefixed
  contig IDs to match the reference build.
* Variants in ``chr<N> POS REF ALT`` form, with sample genotype as ``0/0`` /
  ``0/1`` / ``1/1`` / ``./.`` derived from ``consensus_genotypes.dosage``.
* gzipped (``.vcf.gz``). Beagle 5.5 accepts both gzip and bgzip.

This module deliberately writes text + ``gzip.open`` rather than going through
``cyvcf2.Writer``. The Writer requires a template header VCF, and constructing
that template from nothing is ironic given we have a clean tabular data source.
The text path is small, auditable, and produces byte-identical output across
platforms.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

import structlog

from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.imputation.archive import (
    CHRX_REGIONS,
    ChrxRegion,
    ImputationArchive,
    restrict_file,
)
from genome.imputation.runs import insert_run
from genome.imputation.sex import profile_sex_label
from genome.par_regions import PAR1_END, is_nonpar

if TYPE_CHECKING:
    from pathlib import Path

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

EXPORT_PIPELINE_VERSION: Final[str] = "imputation_prepare_v0.1.0"
"""Pipeline version stamped on ``imputation_runs.pipeline_version`` for prepare."""

_IMPUTATION_SERVER: Final[str] = "beagle"
_DEFAULT_PANEL: Final[str] = "1000g_phase3_grch38"

# Per-chromosome lengths for GRCh38 (used in ``##contig=<...>`` headers).
# Values from the GRCh38.p14 primary assembly; lengths are not strictly
# required by Beagle but make the VCF valid against strict validators.
_CONTIG_LENGTHS_GRCH38: Final[dict[str, int]] = {
    "1": 248_956_422,
    "2": 242_193_529,
    "3": 198_295_559,
    "4": 190_214_555,
    "5": 181_538_259,
    "6": 170_805_979,
    "7": 159_345_973,
    "8": 145_138_636,
    "9": 138_394_717,
    "10": 133_797_422,
    "11": 135_086_622,
    "12": 133_275_309,
    "13": 114_364_328,
    "14": 107_043_718,
    "15": 101_991_189,
    "16": 90_338_345,
    "17": 83_257_441,
    "18": 80_373_285,
    "19": 58_617_616,
    "20": 64_444_167,
    "21": 46_709_983,
    "22": 50_818_468,
    "X": 156_040_895,
    "Y": 57_227_415,
    "MT": 16_569,
}

_AUTOSOMES: Final[tuple[str, ...]] = tuple(str(i) for i in range(1, 23))
_IMPUTABLE_CHROMS: Final[tuple[str, ...]] = (*_AUTOSOMES, "X", "Y")
"""Chromosomes exported for imputation: autosomes + X + Y. MT is not imputed."""


@dataclass(frozen=True, slots=True)
class PreparedUpload:
    """Result of :func:`prepare_run` — what landed on disk and what's in the DB."""

    imputation_id: int
    archive: ImputationArchive
    vcf_paths: tuple[Path, ...]
    variants_total: int
    variants_per_chrom: dict[str, int]
    manifest_path: Path
    input_run_ids: tuple[int, ...]
    profile_sex: str
    chrx_regions: dict[str, int]
    """Per-region chrX target counts (``par1`` / ``nonpar`` / ``par2``); empty when
    no chrX rows were exported (PR 5a / M3-physical)."""


def _input_run_ids(conn: DuckDBPyConnection) -> tuple[int, ...]:
    """Return the distinct ``ingestion_runs.run_id`` values whose active calls
    will feed the merged consensus. Used as the lineage record for the
    imputation run.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ingestion_run_id
          FROM genotype_calls
         WHERE is_active
           AND source IN ('23andme', 'ancestry')
         ORDER BY ingestion_run_id
        """,
    ).fetchall()
    return tuple(int(r[0]) for r in rows)


@dataclass(frozen=True, slots=True)
class _ExportRow:
    """Pre-pivoted row staged for one variant going into the VCF.

    Fields are the bare minimum a VCF needs: contig, 1-based position, optional
    rsID, ref + alt, and a sample genotype as ``0/0`` / ``0/1`` / ``1/1`` /
    ``./.``.
    """

    chrom: str
    pos: int
    rsid: str | None
    ref: str
    alt: str
    genotype: str
    dosage: int | None = None
    """Raw alt-allele dosage (0/1/2), carried so the chrX path can re-render a male
    non-PAR row as haploid (PR 5a). ``None`` / unused for the autosomal path."""


def _genotype_for_dosage(dosage: int | None, *, is_no_call: bool) -> str:
    """Map ``consensus_genotypes.dosage`` (alt count) to a VCF GT string.

    Schema invariant: ``dosage`` is 0 / 1 / 2 for hom-ref / het / hom-alt, or
    NULL when ``is_no_call`` is true. We render unphased (``/``) because the
    raw chip data has no phasing information — Beagle phases internally as
    part of imputation.
    """
    if is_no_call or dosage is None:
        return "./."
    if dosage == 0:
        return "0/0"
    if dosage == 1:
        return "0/1"
    if dosage == 2:  # noqa: PLR2004 — explicit branch for clarity
        return "1/1"
    msg = f"invalid dosage value: {dosage}"
    raise ValueError(msg)


def _haploid_genotype_for_dosage(dosage: int | None, *, is_no_call: bool) -> str:
    """Map ``dosage`` to a HAPLOID VCF GT for a male non-PAR chrX call (PR 5a / M3).

    A hemizygous male carries a single allele in non-PAR: dosage 0 → ``'0'``,
    2 → ``'1'``. A no-call, a missing dosage, or a biologically-impossible male
    non-PAR het (dosage 1 — a hemizygous position cannot be heterozygous) renders
    as the haploid no-call ``'.'`` so Beagle imputes it (finding-029 Decision 2).
    Always single-token — never ``'./.'`` — so the region file is uniform-haploid.
    """
    if is_no_call or dosage is None:
        return "."
    if dosage == 0:
        return "0"
    if dosage == 2:  # noqa: PLR2004 — explicit branch for clarity
        return "1"
    if dosage == 1:
        # Impossible male non-PAR het — emit a haploid no-call and let Beagle impute.
        return "."
    msg = f"invalid dosage value: {dosage}"
    raise ValueError(msg)


def _chrx_region_for_pos(pos: int) -> ChrxRegion:
    """Bucket a 1-based GRCh38 chrX position into ``par1`` / ``nonpar`` / ``par2``.

    Uses :func:`genome.par_regions.is_nonpar` for the PAR/non-PAR split (so the two
    telomeric slivers outside the PAR windows correctly count as non-PAR), then
    splits PAR by ``PAR1_END``. Boundaries live in :mod:`genome.par_regions`, so
    there is no SQL predicate to drift against (PR 5a Task 2).
    """
    if is_nonpar(pos):
        return "nonpar"
    return "par1" if pos <= PAR1_END else "par2"


def _bucket_chrx_rows(
    rows: list[_ExportRow], *, profile_sex: str
) -> dict[ChrxRegion, list[_ExportRow]]:
    """Split chrX export rows into the three regions, sex-aware (PR 5a / M3).

    A male profile re-renders its non-PAR rows as haploid (so Beagle imputes the
    biologically-correct hemizygous representation against the native panel
    subset); PAR rows stay diploid for everyone, and a female / ambiguous profile
    renders the whole chromosome diploid (the pre-rendered genotype from
    :func:`_fetch_export_rows`). Each returned region list is uniform-ploidy.
    """
    buckets: dict[ChrxRegion, list[_ExportRow]] = {region: [] for region in CHRX_REGIONS}
    male = profile_sex == "M"
    for row in rows:
        region = _chrx_region_for_pos(row.pos)
        if male and region == "nonpar":
            haploid = _haploid_genotype_for_dosage(row.dosage, is_no_call=False)
            buckets[region].append(replace(row, genotype=haploid))
        else:
            buckets[region].append(row)
    return buckets


def _fetch_export_rows(
    conn: DuckDBPyConnection,
    chrom: str,
) -> list[_ExportRow]:
    """Pull the export rows for one chromosome in position order.

    Filters at SQL-level: only SNVs (Beagle cannot impute INDELs unless they're
    in the reference panel — and 23andMe's I/D indels don't carry the
    sequence anyway), only consensus rows that are not no-call (no-calls add
    no information for imputation), only variants whose ref/alt are single
    bases (so we never emit a malformed VCF row), and only variants where
    ``ref != alt``.

    The last filter excludes positions where the user is homozygous and we
    have no reference panel to identify the canonical allele. Phase 2's
    alphabetical-ordering rule sets both ref and alt to the same base for
    these positions (an honest "we don't know which is the reference"
    encoding); Beagle cannot impute against ``ref=alt`` rows. The downstream
    impact is that homozygous-only positions are dropped from the export, but
    Beagle still has the polymorphic positions (het + hom-alt) to impute
    against — once Phase 5 loads dbSNP, a future prepare step can rewrite
    these positions with the canonical REF/ALT and recover the dropped rows.
    """
    rows = conn.execute(
        """
        SELECT
            vm.pos_grch38,
            vm.rsid,
            vm.ref_allele,
            vm.alt_allele,
            cg.dosage,
            cg.is_no_call
          FROM consensus_genotypes cg
          JOIN variants_master vm ON vm.variant_id = cg.variant_id
         WHERE CAST(vm.chrom AS VARCHAR) = ?
           AND vm.variant_type = 'SNV'
           AND length(vm.ref_allele) = 1
           AND length(vm.alt_allele) = 1
           AND vm.ref_allele != vm.alt_allele
           AND NOT cg.is_no_call
         ORDER BY vm.pos_grch38
        """,
        [chrom],
    ).fetchall()
    return [
        _ExportRow(
            chrom=chrom,
            pos=int(pos),
            rsid=None if rsid is None else str(rsid),
            ref=str(ref),
            alt=str(alt),
            genotype=_genotype_for_dosage(
                None if dosage is None else int(dosage),
                is_no_call=bool(is_no_call),
            ),
            dosage=None if dosage is None else int(dosage),
        )
        for pos, rsid, ref, alt, dosage, is_no_call in rows
    ]


def _vcf_header(chrom: str, sample_id: str) -> str:
    """Build the VCF header for one chromosome's export file.

    The header lists *only* the contig being exported. Strict validators require
    every record's contig to be declared, but do not require every chromosome
    in the reference to be listed in every file.
    """
    length = _CONTIG_LENGTHS_GRCH38[chrom]
    return (
        "##fileformat=VCFv4.2\n"
        "##source=genome.imputation.vcf_export\n"
        "##reference=GRCh38\n"
        f"##contig=<ID=chr{chrom},length={length},assembly=GRCh38>\n"
        '##INFO=<ID=.,Number=0,Type=Flag,Description="No INFO emitted">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t{sample_id}\n"
    )


def _write_chromosome_vcf(
    path: Path,
    rows: list[_ExportRow],
    *,
    sample_id: str,
) -> None:
    """Write the VCF for one chromosome to ``path`` (gzipped).

    ``rows`` must be sorted by position. The caller (``_fetch_export_rows``)
    sorts at SQL level so we trust the order here.
    """
    if not rows:
        return
    chrom = rows[0].chrom
    with gzip.open(path, "wt", encoding="ascii", compresslevel=6) as out:
        out.write(_vcf_header(chrom, sample_id))
        for r in rows:
            rsid = r.rsid or "."
            out.write(
                f"chr{r.chrom}\t{r.pos}\t{rsid}\t{r.ref}\t{r.alt}\t.\tPASS\t.\tGT\t{r.genotype}\n",
            )


def _write_chrx_region_targets(
    archive: ImputationArchive,
    chrx_region_rows: dict[ChrxRegion, list[_ExportRow]],
    *,
    sample_id: str,
    log: structlog.stdlib.BoundLogger,
) -> list[Path]:
    """Write one chrX target VCF per non-empty region; return the written paths (PR 5a).

    Each region file lands under ``upload/chrX_regions/`` and is uniform-ploidy
    (the bucketing in :func:`_bucket_chrx_rows` already rendered male non-PAR rows
    haploid). An empty region is skipped — no file, recorded as 0 in the manifest.
    """
    written: list[Path] = []
    for region in CHRX_REGIONS:
        region_rows = chrx_region_rows.get(region, [])
        if not region_rows:
            continue
        path = archive.chrx_region_upload_path(region)
        _write_chromosome_vcf(path, region_rows, sample_id=sample_id)
        restrict_file(path)
        written.append(path)
        log.info(
            "imputation.prepare.chrx_region",
            region=region,
            variants=len(region_rows),
            path=str(path),
        )
    return written


def _write_manifest(  # noqa: PLR0913 — manifest covers every provenance field; flat keyword list reads better than a wrapping struct
    archive: ImputationArchive,
    *,
    imputation_id: int,
    sample_id: str,
    panel: str,
    server: str,
    pipeline_version: str,
    variants_per_chrom: dict[str, int],
    input_run_ids: tuple[int, ...],
    profile_sex: str,
    chrx_regions: dict[str, int],
    chrx_ploidy: str | None,
) -> Path:
    """Write a JSON manifest of the prepare step's output.

    The manifest is what a re-running session reads to recover the run
    parameters without re-querying the DB. It is the on-disk source of truth
    for "what did this run upload". ``profile_sex``, ``chrx_regions`` (the
    M3-physical per-region target counts) and ``chrx_ploidy`` (the chrX target
    ploidy decision) are recorded as transient provenance for the chrX path
    (PR 5a) — no DB column is written.
    """
    payload: dict[str, object] = {
        "imputation_id": imputation_id,
        "sample_id": sample_id,
        "reference_panel": panel,
        "imputation_server": server,
        "imputation_tool": "beagle_5.5",
        "pipeline_version": pipeline_version,
        "build": "GRCh38",
        "compression": "gzip",
        "profile_sex": profile_sex,
        "variants_per_chrom": variants_per_chrom,
        "variants_total": sum(variants_per_chrom.values()),
        "chromosomes_exported": sorted(variants_per_chrom),
        "input_run_ids": list(input_run_ids),
        "chrx_regions": chrx_regions,
        "chrx_ploidy": chrx_ploidy,
    }
    archive.upload_manifest.write_text(json.dumps(payload, indent=2, sort_keys=True))
    restrict_file(archive.upload_manifest)
    return archive.upload_manifest


def _detect_existing_prepared_run(conn: DuckDBPyConnection) -> int | None:
    """Return an ``imputation_id`` for a prior prepare we should reuse, or ``None``.

    Re-running ``prepare`` when an existing run is already in ``status='pending'``
    or ``'processing'`` is unusual — only one in-flight imputation run is
    expected at a time. We return the existing id so the caller can decide
    whether to abort or proceed (the CLI surfaces this as a clear message).
    """
    row = conn.execute(
        """
        SELECT imputation_id
          FROM imputation_runs
         WHERE status IN ('pending', 'processing')
         ORDER BY imputation_id DESC
         LIMIT 1
        """,
    ).fetchone()
    return None if row is None else int(row[0])


def prepare_run(
    *,
    sample_id: str = "sample",
    duckdb_path: Path | None = None,
    archive_root: Path | None = None,
    force_new: bool = False,
    sex: str | None = None,
) -> PreparedUpload:
    """Build the per-chromosome upload VCFs and create an ``imputation_runs`` row.

    Idempotence: if a ``pending`` / ``processing`` run already exists, raises
    ``RuntimeError`` unless ``force_new=True``. ``force_new`` should be used
    sparingly (it does not invalidate the prior row — it creates a new one
    alongside, so the user can decide which to ship).

    Parameters
    ----------
    sample_id : the name to use in the VCF sample column. Default ``'sample'``.
    duckdb_path : override the analytical DB path; defaults to settings.
    archive_root : override the archive root; defaults to settings.
    force_new : create a new run even if an in-flight run exists.
    sex : explicit ``'M'`` / ``'F'`` override, or ``None`` for ``auto`` (resolve
        from chip ``sample_qc``). Recorded in the manifest as transient chrX
        provenance only — no DB column is written, and an ambiguous result does
        not block the prepare (the export itself is sex-independent under R1).
    """
    settings = get_settings()
    archive_root = archive_root or settings.archive_path
    duckdb_path = duckdb_path or settings.genome_duckdb_path

    log = logger.bind(sample_id=sample_id)
    log.info("imputation.prepare.start")

    with duckdb_connection(duckdb_path) as conn:
        existing = _detect_existing_prepared_run(conn)
        if existing is not None and not force_new:
            msg = (
                f"imputation run {existing} is already in flight "
                f"(pending or processing); pass force_new=True to add another"
            )
            raise RuntimeError(msg)

        input_run_ids = _input_run_ids(conn)
        if not input_run_ids:
            msg = (
                "no active 23andMe or Ancestry calls found; ingest at least one "
                "raw export before running `genome imputation prepare`"
            )
            raise RuntimeError(msg)

        # Resolved softly (never raises on ambiguity): the export is
        # sex-independent under R1, so this is recorded as manifest provenance
        # only — it must not block an ambiguous-sex profile from preparing.
        profile_sex = profile_sex_label(conn, sex)

        # Compute totals up front so the imputation_runs row records the right number.
        variants_per_chrom: dict[str, int] = {}
        export_rows_per_chrom: dict[str, list[_ExportRow]] = {}
        chrx_region_rows: dict[ChrxRegion, list[_ExportRow]] = {}
        for chrom in _IMPUTABLE_CHROMS:
            rows = _fetch_export_rows(conn, chrom)
            if not rows:
                continue
            if chrom == "X":
                # M3-physical: bucket chrX into PAR1 / non-PAR / PAR2 region targets,
                # rendering male non-PAR haploid. variants_per_chrom["X"] stays the
                # whole-chromosome total (= sum of regions).
                chrx_region_rows = _bucket_chrx_rows(rows, profile_sex=profile_sex)
                variants_per_chrom["X"] = len(rows)
                continue
            export_rows_per_chrom[chrom] = rows
            variants_per_chrom[chrom] = len(rows)
        total_variants = sum(variants_per_chrom.values())

        if total_variants == 0:
            msg = (
                "no eligible SNV consensus rows found; "
                "run `genome merge` after ingest to produce the consensus set"
            )
            raise RuntimeError(msg)

        imputation_id = insert_run(
            conn,
            input_run_ids=input_run_ids,
            imputation_server=_IMPUTATION_SERVER,
            reference_panel=_DEFAULT_PANEL,
            pipeline_version=EXPORT_PIPELINE_VERSION,
            variants_input=total_variants,
        )

    archive = ImputationArchive.for_run(archive_root, imputation_id)
    archive.ensure_layout()

    written: list[Path] = []
    for chrom, rows in export_rows_per_chrom.items():
        path = archive.upload_vcf_path(chrom)
        _write_chromosome_vcf(path, rows, sample_id=sample_id)
        restrict_file(path)
        written.append(path)
        log.info(
            "imputation.prepare.chrom",
            chrom=chrom,
            variants=variants_per_chrom[chrom],
            path=str(path),
        )

    # M3-physical chrX: one target file per non-empty region, under chrX_regions/.
    written.extend(
        _write_chrx_region_targets(archive, chrx_region_rows, sample_id=sample_id, log=log)
    )

    chrx_regions: dict[str, int] = (
        {str(region): len(chrx_region_rows.get(region, [])) for region in CHRX_REGIONS}
        if chrx_region_rows
        else {}
    )
    chrx_ploidy = (
        ("male_nonpar_haploid" if profile_sex == "M" else "diploid") if chrx_region_rows else None
    )

    manifest = _write_manifest(
        archive,
        imputation_id=imputation_id,
        sample_id=sample_id,
        panel=_DEFAULT_PANEL,
        server=_IMPUTATION_SERVER,
        pipeline_version=EXPORT_PIPELINE_VERSION,
        variants_per_chrom=variants_per_chrom,
        input_run_ids=input_run_ids,
        profile_sex=profile_sex,
        chrx_regions=chrx_regions,
        chrx_ploidy=chrx_ploidy,
    )

    log.info(
        "imputation.prepare.complete",
        imputation_id=imputation_id,
        variants_total=total_variants,
        chroms_exported=sorted(variants_per_chrom),
        profile_sex=profile_sex,
        chrx_regions=chrx_regions,
        chrx_ploidy=chrx_ploidy,
    )
    return PreparedUpload(
        imputation_id=imputation_id,
        archive=archive,
        vcf_paths=tuple(written),
        variants_total=total_variants,
        variants_per_chrom=dict(variants_per_chrom),
        manifest_path=manifest,
        input_run_ids=input_run_ids,
        profile_sex=profile_sex,
        chrx_regions=chrx_regions,
    )
