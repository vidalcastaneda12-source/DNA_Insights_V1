"""Stream imputed VCFs into the analytical DB.

This is the heavy step of Phase 4. The Beagle 5.5 result is several million
variants spread across one VCF per chromosome. We stream each file through
cyvcf2, batch records into PyArrow Tables, and bulk-insert with the locked
DuckDB convention (registered Arrow Table + ``INSERT ... SELECT``; see
``finding-004``).

Key design points:

* **Bypass lift-over.** Beagle output is GRCh38-native. We use
  :class:`IdentityLiftover`-equivalent logic (the lifted positions are the
  same as the input positions).
* **Capture INFO/DR2 per variant.** Beagle emits dosage R² (``DR2``) as the
  imputation quality score; we accept ``R2`` and ``Rsq`` as fallbacks for
  compatibility with other servers. This drives every downstream filter
  ("only use variants with R² > 0.3", etc.).
* **R² threshold filter at import time.** Variants with R² below
  ``r2_threshold`` (default 0.3) are skipped entirely and never written to
  ``genotype_calls``. The threshold is recorded on the run row.
* **Add missing variants to ``variants_master``.** Most imputed variants are
  not in the chip-genotyped set, so we expand the master table.
* **Stream per chromosome.** We never load the full result into memory.
* **Compute a sample QC row.** Call rate should be ~100% (imputation fills
  every position), but het rate and sex from imputed X/Y are useful.

Schema fields we write:

* ``variants_master``: rsid, chrom, pos_grch38, ref_allele, alt_allele,
  variant_type (SNV — INDELs are not in the standard imputation panel),
  has_imputed_call = TRUE.
* ``genotype_calls``: variant_id, source='beagle_imputed', is_imputed=TRUE,
  imputation_r2 = INFO/DR2 (or R2/Rsq fallback),
  imputation_panel='1000g_phase3_grch38' (default),
  allele_1/allele_2 derived from GT, is_no_call inferred from missing GT,
  strand_status = 'resolved_plus' (Beagle output is on the forward strand).
* ``sample_qc``: one row per ``ingestion_runs`` row we create for the
  imputed source.
* ``ingestion_runs``: one row per import; ``source='beagle_imputed'``.
* ``imputation_runs``: update ``variants_output``, ``mean_r2``,
  ``variants_above_r2_0_3``, ``variants_above_r2_0_8``, ``r2_threshold``.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

import pyarrow as pa
import structlog

from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.imputation.archive import ImputationArchive
from genome.imputation.bgzf import is_truncated_bgzf
from genome.imputation.runs import (
    ImputationRun,
    fetch_run,
    record_import_volumes,
    update_status,
)
from genome.ingest.writer import insert_ingestion_run
from genome.par_regions import is_nonpar

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import cyvcf2 as _cyvcf2_typing
    from duckdb import DuckDBPyConnection


logger = structlog.get_logger(__name__)

IMPUTATION_PIPELINE_VERSION: Final[str] = "imputation_import_v0.1.0"
DEFAULT_BATCH_SIZE: Final[int] = 50_000
DEFAULT_R2_THRESHOLD: Final[float] = 0.3
_R2_THRESHOLDS: Final[tuple[float, float]] = (0.3, 0.8)

# Male hemizygous non-PAR chrX has no cross-sample dosage variance for a single
# sample, so Beagle's INFO/DR2 is structurally 0.00 there (finding-031) — a dead
# metric, not a quality signal. For that regime we gate on dosage-confidence
# instead: ``max(DS, 1 - DS)`` over the haploid FORMAT/DS, which equals Beagle's
# max genotype-posterior for a hemizygous call. Typed anchors carry an integer DS
# (dconf = 1.0) and so always survive — this is what fixes the acute regression
# where the DR2 gate dropped even the user's own non-PAR genotypes.
DEFAULT_DCONF_THRESHOLD: Final[float] = 0.9
# Provenance marker appended to ``genotype_calls.quality_flags`` for rows whose
# ``imputation_r2`` carries a dosage-confidence value rather than a DR2 (the
# documented overload, finding-031). Keeps the DR2 run-counters uncontaminated
# and the overloaded rows queryable.
DCONF_QUALITY_FLAG: Final[str] = "nonpar_dosage_conf"
# Re-diploidized male non-PAR DS is on the 0..1 haploid scale (the re-diploidizer
# copies the haploid DS verbatim onto the 1|1 GT — chrx_panel.py:69-82). A value
# above 1 means the seam changed and DS is now diploid-scaled (0..2); that must
# fail loudly rather than silently mis-gate, so we assert DS <= 1 + this epsilon.
_DS_SCALE_EPS: Final[float] = 1e-4
# Empirical: ~30M variants stream in ~30 min on a dev machine
# (the benchmark test confirms 1M rows clear in well under 60s).
# Rate used for the dry-run time estimate.
_ESTIMATED_VARIANTS_PER_SECOND: Final[int] = 16_500

_IMPUTABLE_CHROMS: Final[frozenset[str]] = frozenset(
    {*(str(i) for i in range(1, 23)), "X", "Y"},
)

_DBSNP_RSID_RE: Final[re.Pattern[str]] = re.compile(r"^rs[0-9]+$")


def _dbsnp_rsid_or_none(vcf_id: str | None) -> str | None:
    """Keep only a strict dbSNP rs identifier; NULL anything else (finding-021).

    Beagle emits a synthetic ``chrom:pos:ref:alt`` string in the VCF ID field for
    panel variants with no dbSNP rsID (e.g. ``14:29619977:C:T``). Copied verbatim
    that coordinate string would masquerade as an rsid in ``variants_master.rsid``,
    so store the value only when it is a real ``rs<n>`` (strict ``^rs[0-9]+$``),
    else ``None``. NULL is lossless — the coordinate is reconstructable from
    ``chrom`` / ``pos`` / ``ref`` / ``alt``.
    """
    if vcf_id is None:
        return None
    return vcf_id if _DBSNP_RSID_RE.match(vcf_id) else None


@dataclass(slots=True)
class _ImportCounters:
    """Mutable accumulators threaded through the streaming ingest."""

    variants_total: int = 0
    variants_called: int = 0
    variants_no_call: int = 0
    variants_above_r2_0_3: int = 0
    variants_above_r2_0_8: int = 0
    variants_below_threshold: int = 0
    # Male non-PAR chrX rows kept on the dosage-confidence gate (finding-031).
    # Reported separately so the DR2 run-stats stay pure; this is the positive
    # yield signal for the chrX gate (expected order 10^4-10^5).
    nonpar_confident: int = 0
    r2_sum: float = 0.0
    r2_count: int = 0
    autosomal_called: int = 0
    autosomal_het: int = 0
    x_called: int = 0
    x_het: int = 0
    y_called: int = 0
    per_chrom: dict[str, int] = field(default_factory=dict)
    # Records actually read from each chromosome's VCF *before* any biallelic /
    # R²-threshold filter. The empty-output guard (finding-008 #2) keys on this:
    # a chromosome whose file streamed zero raw records despite a non-trivial
    # prepare-manifest upload count is a silent Beagle failure.
    raw_per_chrom: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _Batch:
    """One batch of variant rows accumulating before a bulk insert."""

    rsid: list[str | None] = field(default_factory=list)
    chrom: list[str] = field(default_factory=list)
    pos: list[int] = field(default_factory=list)
    ref: list[str] = field(default_factory=list)
    alt: list[str] = field(default_factory=list)
    allele_1: list[str | None] = field(default_factory=list)
    allele_2: list[str | None] = field(default_factory=list)
    is_no_call: list[bool] = field(default_factory=list)
    imputation_r2: list[float | None] = field(default_factory=list)
    # Per-row ``quality_flags`` (``VARCHAR[]``): ``[DCONF_QUALITY_FLAG]`` for a
    # dosage-confidence-gated male non-PAR row, ``None`` otherwise (finding-031).
    quality_flags: list[list[str] | None] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.pos)

    def clear(self) -> None:
        self.rsid.clear()
        self.chrom.clear()
        self.pos.clear()
        self.ref.clear()
        self.alt.clear()
        self.allele_1.clear()
        self.allele_2.clear()
        self.is_no_call.clear()
        self.imputation_r2.clear()
        self.quality_flags.clear()


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Summary returned by :func:`import_result`."""

    imputation_id: int
    ingestion_run_id: int
    qc_id: int
    variants_total: int
    variants_called: int
    variants_no_call: int
    variants_below_threshold: int
    new_variants_master_rows: int
    deactivated_prior_calls: int
    mean_r2: float | None
    variants_above_r2_0_3: int
    variants_above_r2_0_8: int
    r2_threshold: float
    dconf_threshold: float
    nonpar_confident: int
    profile_sex: str | None
    chromosomes_imported: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DryRunResult:
    """Summary returned by :func:`import_result` when ``dry_run=True``.

    Reports the per-chromosome variant counts we would import (after the
    R²-threshold and chromosome filters), plus a wall-clock time estimate
    based on the documented benchmark. No database writes happen on this path.
    """

    imputation_id: int
    chromosomes_planned: tuple[str, ...]
    variants_total: int
    variants_below_threshold: int
    per_chrom: dict[str, int]
    r2_threshold: float
    dconf_threshold: float
    profile_sex: str | None
    estimated_seconds: float


def _normalize_chrom_label(chrom: str) -> str | None:
    """Strip an optional ``chr`` prefix and return the canonical label.

    Returns ``None`` for anything outside ``_IMPUTABLE_CHROMS``. Beagle's
    output uses ``chr1`` / ``chrX`` / etc.; the schema's enum is the unprefixed
    label.
    """
    raw = chrom.strip().upper().removeprefix("CHR")
    if raw in _IMPUTABLE_CHROMS:
        return raw
    return None


def _genotype_alleles(
    ref: str,
    alts: tuple[str, ...],
    genotype: tuple[int, int, bool] | None,
) -> tuple[str | None, str | None, bool]:
    """Return ``(allele_1, allele_2, is_no_call)`` from a cyvcf2 genotype tuple.

    cyvcf2 reports genotype as ``[ref_idx, alt_idx, phased_flag]``. A missing
    call is ``[-1, -1, True/False]``. Imputed positions almost always have a
    genotype (R² captures uncertainty separately), but defensive handling here
    is cheap.
    """
    if genotype is None:
        return None, None, True
    a_idx, b_idx, _phased = genotype
    if a_idx < 0 or b_idx < 0:
        return None, None, True

    def _allele(idx: int) -> str:
        if idx == 0:
            return ref
        # cyvcf2's index 1.. refers to the n-th ALT allele (1-based).
        return alts[idx - 1] if idx - 1 < len(alts) else ref

    return _allele(a_idx), _allele(b_idx), False


def _is_biallelic_snv(ref: str, alts: tuple[str, ...]) -> bool:
    """Return True iff the variant is a single biallelic SNV (one base each)."""
    return len(alts) == 1 and len(ref) == 1 and len(alts[0]) == 1


def _extract_r2(info: _cyvcf2_typing.INFO) -> float | None:
    """Return the imputation R² for this variant, or ``None`` if absent.

    Beagle 5.5's INFO field is ``DR2`` (dosage R²). Some servers (TopMed)
    emit ``R2``; older Minimac releases use ``Rsq``. We try the keys in
    preference order so the import path is reusable across servers.
    """
    for key in ("DR2", "R2", "Rsq", "INFO_R2"):
        value = info.get(key)
        if value is not None:
            with contextlib.suppress(TypeError, ValueError):
                return float(value)
    return None


def _extract_ds(variant: _cyvcf2_typing.Variant) -> float | None:
    """Return ``FORMAT/DS`` (alt dosage) for the single sample, or ``None``.

    cyvcf2 returns the per-sample DS as a ``(n_samples, 1)`` float32 array. It
    *raises ``KeyError``* when the DS tag is not declared in the file header (the
    chip / DR2-only fixtures), and returns ``None`` (or NaN at ``[0][0]``) when
    the tag is declared but absent for this record/sample. All three map to
    ``None`` here. We read sample 0 only — the imputation pipeline is
    single-sample throughout. ``None`` lets the male non-PAR branch of
    :func:`_variant_quality` fail closed rather than mis-gate.
    """
    try:
        arr = variant.format("DS")
    except KeyError:
        return None
    if arr is None or arr.size == 0:
        return None
    value = float(arr[0][0])
    if math.isnan(value):
        return None
    return value


def _extract_imp(variant: _cyvcf2_typing.Variant) -> bool:
    """Return True iff the variant carries Beagle's ``INFO/IMP`` flag (imputed site).

    Beagle stamps ``IMP`` on sites it **imputed** and omits it on **typed**
    (genotyped) sites carried over from the target. The male non-PAR gate uses
    this to keep every typed anchor (observed → always retained) while gating
    imputed sites on confident-ALT dosage (finding-031). A VCF that declares no
    ``IMP`` (non-Beagle output / fixtures) yields ``False`` everywhere, so its
    male non-PAR rows are treated as observed and kept — the conservative
    anchor-retaining default.
    """
    try:
        return variant.INFO.get("IMP") is not None
    except KeyError:
        return False


@dataclass(frozen=True, slots=True)
class _QualityVerdict:
    """Outcome of the per-variant import gate (:func:`_variant_quality`)."""

    keep: bool
    quality: float | None
    """The value to store in ``genotype_calls.imputation_r2``: the DR2 on the
    DR2 path (may be ``None``), or the dosage-confidence on the dconf path."""
    is_dconf: bool
    """True when ``quality`` is a dosage-confidence (male non-PAR), so the caller
    appends :data:`DCONF_QUALITY_FLAG` and keeps it out of the DR2 counters."""


def _variant_quality(  # noqa: PLR0913 — the shared gate needs full variant context + both thresholds
    *,
    chrom: str,
    pos: int,
    profile_sex: str | None,
    r2: float | None,
    ds: float | None,
    is_imputed: bool,
    r2_threshold: float,
    dconf_threshold: float,
) -> _QualityVerdict:
    """The single keep/drop + quality decision shared by import and dry-run.

    Both :func:`_stream_chromosome` (the writing path) and
    :func:`_count_chromosome_variants` (the dry-run count) route through here so
    the two cannot diverge.

    For a **male, non-PAR chrX** variant (``chrom == 'X'`` ∧ ``profile_sex == 'M'``
    ∧ :func:`genome.par_regions.is_nonpar`) the DR2 is structurally 0.00
    (finding-031), so the gate uses the live dosage signal instead, keeping the
    **informative** subset:

    * a **typed** site (``is_imputed`` False — no ``INFO/IMP``) is the user's own
      observed genotype and is **always kept** (anchor retention);
    * an **imputed** site is kept iff it is a **confident ALT-bearing** call,
      ``DS >= dconf_threshold``. Confident hom-ref imputed (``DS`` near 0) and the
      uncertain middle are dropped — the user is ref-by-default there and keeping
      ~2.2M confident hom-ref imputed rows would balloon the corpus for little
      insight (the decision recorded in finding-031).

    Either way the stored ``quality`` is the dosage-confidence ``max(DS, 1 - DS)``
    (= ``DS`` for a kept confident-ALT call, ``1.0`` for a typed anchor). A missing
    DS fails closed (raises); a DS above the 0..1 haploid scale trips the scale
    guard (raises) rather than silently mis-gating. Every other variant —
    autosomes, male PAR, and the genuinely-diploid female X — keeps the existing
    DR2 gate (keep iff ``r2 is None or r2 >= r2_threshold``).
    """
    if chrom == "X" and profile_sex == "M" and is_nonpar(pos):
        if ds is None:
            msg = (
                f"male non-PAR chrX variant at chrX:{pos} has no FORMAT/DS; the "
                "dosage-confidence gate cannot evaluate it. Refusing to fall back "
                "to the (structurally dead) DR2 gate, which would drop it "
                "(finding-031)."
            )
            raise RuntimeError(msg)
        if ds > 1.0 + _DS_SCALE_EPS:
            msg = (
                f"male non-PAR chrX DS={ds} at chrX:{pos} exceeds the 0..1 haploid "
                "scale; the re-diploidize seam must have changed (DS now diploid-"
                "scaled). Refusing to mis-gate (finding-031)."
            )
            raise RuntimeError(msg)
        dconf = max(ds, 1.0 - ds)
        # Typed anchor → always kept; imputed → only a confident ALT-bearing call.
        keep = True if not is_imputed else ds >= dconf_threshold
        return _QualityVerdict(keep=keep, quality=dconf, is_dconf=True)

    keep = r2 is None or r2 >= r2_threshold
    return _QualityVerdict(keep=keep, quality=r2, is_dconf=False)


def _assert_result_vcf_intact(path: Path) -> None:
    """Refuse a truncated BGZF result VCF (finding-008 #2).

    When Beagle fails mid-run — e.g. the chrX reference-panel ploidy error — it
    leaves a BGZF ``result/chr*.vcf.gz`` with no EOF marker. cyvcf2 reads such a
    file as zero (or partially-zero) variants with only a warning, so a broken
    run would otherwise import as a silent empty success. Raise instead.
    """
    if is_truncated_bgzf(path):
        msg = (
            f"result VCF {path} is a truncated BGZF file (missing its EOF "
            "marker); the imputation runner almost certainly failed mid-write "
            "(e.g. the chrX reference-panel ploidy failure, finding-008). "
            "Re-run `genome imputation run <id>`, or import the intact "
            "chromosomes with `--chromosomes` (excluding the truncated one)."
        )
        raise RuntimeError(msg)


def _open_imputed_vcf(path: Path) -> _cyvcf2_typing.VCF:
    """Open ``path`` with cyvcf2; refuse a truncated BGZF result first.

    Import is deferred so the type hint stays clean. The truncation guard runs
    before cyvcf2 sees the file, which would otherwise read a truncated result
    as a silent zero-variant success (finding-008 #2).
    """
    _assert_result_vcf_intact(path)
    import cyvcf2  # noqa: PLC0415 — import deferred so module loads without cyvcf2 at type-check time

    return cyvcf2.VCF(str(path))


def _update_zygosity_counters(
    counters: _ImportCounters,
    *,
    chrom: str,
    allele_1: str | None,
    allele_2: str | None,
) -> None:
    """Bump the sex / het counters for one called variant.

    Split out so :func:`_stream_chromosome` does not hit ruff's complexity
    cap. The function is unconditional given the caller has already gated on
    ``is_no_call=False``.
    """
    het = allele_1 != allele_2
    if chrom == "X":
        counters.x_called += 1
        if het:
            counters.x_het += 1
    elif chrom == "Y":
        counters.y_called += 1
    elif chrom != "MT":
        counters.autosomal_called += 1
        if het:
            counters.autosomal_het += 1


def _update_r2_counters(counters: _ImportCounters, r2: float | None) -> None:
    if r2 is None:
        return
    counters.r2_sum += r2
    counters.r2_count += 1
    if r2 >= _R2_THRESHOLDS[0]:
        counters.variants_above_r2_0_3 += 1
    if r2 >= _R2_THRESHOLDS[1]:
        counters.variants_above_r2_0_8 += 1


def _accept_variant(  # noqa: PLR0913 — per-variant columns mirror the VCF row shape
    counters: _ImportCounters,
    batch: _Batch,
    *,
    chrom: str,
    pos: int,
    rsid: str | None,
    ref: str,
    alt: str,
    allele_1: str | None,
    allele_2: str | None,
    is_no_call: bool,
    quality: float | None,
    is_dconf: bool,
) -> None:
    """Append one already-validated variant to the batch and update counters.

    ``quality`` is stored verbatim into ``imputation_r2``. When ``is_dconf`` it is
    a dosage-confidence (male non-PAR, finding-031): it is kept out of the DR2
    counters (so ``mean_r2`` / ``variants_above_r2_*`` stay pure DR2) and counted
    in ``nonpar_confident`` instead, and the row is flagged
    :data:`DCONF_QUALITY_FLAG`.
    """
    counters.variants_total += 1
    if is_no_call:
        counters.variants_no_call += 1
    else:
        counters.variants_called += 1
        _update_zygosity_counters(
            counters,
            chrom=chrom,
            allele_1=allele_1,
            allele_2=allele_2,
        )
    if is_dconf:
        counters.nonpar_confident += 1
    else:
        _update_r2_counters(counters, quality)
    counters.per_chrom[chrom] = counters.per_chrom.get(chrom, 0) + 1

    batch.rsid.append(rsid)
    batch.chrom.append(chrom)
    batch.pos.append(pos)
    batch.ref.append(ref)
    batch.alt.append(alt)
    batch.allele_1.append(allele_1)
    batch.allele_2.append(allele_2)
    batch.is_no_call.append(is_no_call)
    batch.imputation_r2.append(quality)
    batch.quality_flags.append([DCONF_QUALITY_FLAG] if is_dconf else None)


def _stream_chromosome(  # noqa: PLR0913 — streaming knobs mirror the import option surface
    path: Path,
    chrom: str,
    counters: _ImportCounters,
    *,
    profile_sex: str | None,
    r2_threshold: float,
    dconf_threshold: float,
    batch_size: int,
) -> Iterator[_Batch]:
    """Yield batches of normalized rows from one chromosome's imputed VCF.

    Each variant is gated by :func:`_variant_quality`: male non-PAR chrX uses the
    dosage-confidence gate (``dconf_threshold``), everything else the DR2 gate
    (``r2_threshold``). Dropped variants don't reach ``variants_master`` or
    ``genotype_calls`` and are counted in ``counters.variants_below_threshold``.
    DR2-path variants missing an R² value pass through (matching the pre-filter
    behavior; rare on Beagle output but defensible for non-Beagle VCFs).
    """
    log = logger.bind(path=str(path), chrom=chrom)
    log.info("imputation.import.chrom.start")
    batch = _Batch()
    reader = _open_imputed_vcf(path)
    try:
        for v in reader:
            mapped = _normalize_chrom_label(str(v.CHROM))
            if mapped != chrom:
                # The file's chromosome doesn't match the expected one — skip
                # silently. Beagle should never produce this, but a misnamed
                # file would otherwise corrupt the per-chrom counters.
                continue
            # Count the raw record *before* the biallelic / quality filters so the
            # empty-output guard can tell "Beagle produced nothing" (a silent
            # failure) apart from "every variant fell below threshold"
            # (legitimate — raw > 0).
            counters.raw_per_chrom[chrom] = counters.raw_per_chrom.get(chrom, 0) + 1
            alts = tuple(str(a) for a in v.ALT or [])
            if not _is_biallelic_snv(str(v.REF), alts):
                continue

            pos = int(v.POS)
            r2 = _extract_r2(v.INFO)
            ds = _extract_ds(v) if chrom == "X" else None
            is_imputed = _extract_imp(v) if chrom == "X" else False
            verdict = _variant_quality(
                chrom=chrom,
                pos=pos,
                profile_sex=profile_sex,
                r2=r2,
                ds=ds,
                is_imputed=is_imputed,
                r2_threshold=r2_threshold,
                dconf_threshold=dconf_threshold,
            )
            if not verdict.keep:
                counters.variants_below_threshold += 1
                continue

            genotypes = v.genotypes or []
            gt: tuple[int, int, bool] | None = None
            if genotypes:
                a, b, phased = genotypes[0]
                gt = (int(a), int(b), bool(phased))
            allele_1, allele_2, is_no_call = _genotype_alleles(
                str(v.REF),
                alts,
                gt,
            )

            _accept_variant(
                counters,
                batch,
                chrom=chrom,
                pos=pos,
                rsid=_dbsnp_rsid_or_none(v.ID),
                ref=str(v.REF),
                alt=alts[0],
                allele_1=allele_1,
                allele_2=allele_2,
                is_no_call=is_no_call,
                quality=verdict.quality,
                is_dconf=verdict.is_dconf,
            )

            if len(batch) >= batch_size:
                yield batch
                batch = _Batch()
    finally:
        reader.close()

    if len(batch) > 0:
        yield batch
    log.info(
        "imputation.import.chrom.complete",
        variants=counters.per_chrom.get(chrom, 0),
    )


def _create_stage_table(conn: DuckDBPyConnection) -> None:
    conn.execute("DROP TABLE IF EXISTS _impute_stage")
    conn.execute(
        """
        CREATE TEMP TABLE _impute_stage (
            ord            BIGINT,
            rsid           VARCHAR,
            chrom          VARCHAR,
            pos_grch38     BIGINT,
            ref_allele     VARCHAR,
            alt_allele     VARCHAR,
            allele_1       VARCHAR,
            allele_2       VARCHAR,
            is_no_call     BOOLEAN,
            imputation_r2  DOUBLE,
            quality_flags  VARCHAR[]
        )
        """,
    )


def _stage_batch(conn: DuckDBPyConnection, batch: _Batch) -> None:
    """Register ``batch`` as an Arrow Table and insert into the stage."""
    if len(batch) == 0:
        return
    n = len(batch)
    table = pa.table(
        {
            "ord": pa.array(range(n), type=pa.int64()),
            "rsid": pa.array(batch.rsid, type=pa.string()),
            "chrom": pa.array(batch.chrom, type=pa.string()),
            "pos_grch38": pa.array(batch.pos, type=pa.int64()),
            "ref_allele": pa.array(batch.ref, type=pa.string()),
            "alt_allele": pa.array(batch.alt, type=pa.string()),
            "allele_1": pa.array(batch.allele_1, type=pa.string()),
            "allele_2": pa.array(batch.allele_2, type=pa.string()),
            "is_no_call": pa.array(batch.is_no_call, type=pa.bool_()),
            "imputation_r2": pa.array(batch.imputation_r2, type=pa.float64()),
            "quality_flags": pa.array(batch.quality_flags, type=pa.list_(pa.string())),
        },
    )
    try:
        conn.register("_impute_stage_arrow", table)
        conn.execute("INSERT INTO _impute_stage SELECT * FROM _impute_stage_arrow")
    finally:
        conn.unregister("_impute_stage_arrow")


def _upsert_variants_master(conn: DuckDBPyConnection) -> int:
    """Add ``_impute_stage`` variants not yet in ``variants_master``; return new count."""
    before = conn.execute("SELECT COUNT(*) FROM variants_master").fetchone()
    before_n = int(before[0]) if before else 0
    conn.execute(
        """
        INSERT INTO variants_master (
            rsid, chrom, pos_grch38, ref_allele, alt_allele,
            variant_type, liftover_chain, liftover_status, has_imputed_call
        )
        SELECT
            ANY_VALUE(s.rsid),
            s.chrom::chromosome_enum,
            s.pos_grch38,
            s.ref_allele,
            s.alt_allele,
            'SNV'::variant_type_enum,
            'native_grch38',
            'native_grch38',
            TRUE
          FROM _impute_stage s
          LEFT JOIN variants_master vm
            ON vm.chrom = s.chrom::chromosome_enum
           AND vm.pos_grch38 = s.pos_grch38
           AND vm.ref_allele = s.ref_allele
           AND vm.alt_allele = s.alt_allele
         WHERE vm.variant_id IS NULL
         GROUP BY s.chrom, s.pos_grch38, s.ref_allele, s.alt_allele
        """,
    )
    after = conn.execute("SELECT COUNT(*) FROM variants_master").fetchone()
    after_n = int(after[0]) if after else 0
    return after_n - before_n


def _refresh_imputed_flag(conn: DuckDBPyConnection) -> None:
    """Set ``has_imputed_call=TRUE`` for any pre-existing variants in this batch.

    New rows already get ``has_imputed_call=TRUE`` at INSERT time. Pre-existing
    rows (chip-genotyped variants that overlap the imputation panel) need the
    flag flipped on.
    """
    conn.execute(
        """
        UPDATE variants_master
           SET has_imputed_call = TRUE
         WHERE variant_id IN (
                SELECT vm.variant_id
                  FROM _impute_stage s
                  JOIN variants_master vm
                    ON vm.chrom = s.chrom::chromosome_enum
                   AND vm.pos_grch38 = s.pos_grch38
                   AND vm.ref_allele = s.ref_allele
                   AND vm.alt_allele = s.alt_allele
           )
        """,
    )


def _deactivate_prior_imputed_calls(
    conn: DuckDBPyConnection,
    *,
    superseded_reason: str,
) -> int:
    """Deactivate any previously-imputed calls at positions in this batch.

    Re-importing an imputation result for the same chromosome supersedes the
    prior imputed calls — same supersession-over-update pattern as the raw
    ingest writer.
    """
    res = conn.execute(
        """
        UPDATE genotype_calls
           SET is_active = FALSE,
               superseded_reason = ?
         WHERE is_active = TRUE
           AND source = 'beagle_imputed'::source_enum
           AND variant_id IN (
                SELECT vm.variant_id
                  FROM _impute_stage s
                  JOIN variants_master vm
                    ON vm.chrom = s.chrom::chromosome_enum
                   AND vm.pos_grch38 = s.pos_grch38
                   AND vm.ref_allele = s.ref_allele
                   AND vm.alt_allele = s.alt_allele
           )
        """,
        [superseded_reason],
    )
    row = res.fetchone() if hasattr(res, "fetchone") else None
    if row is None or row[0] is None:
        return 0
    with contextlib.suppress(TypeError, ValueError):
        return int(row[0])
    return 0


# ``discrepancies`` is the only table that FK-references ``genotype_calls(call_id)``
# (``call_a_id`` / ``call_b_id``; ddl/group_1_genotype.sql). A discrepancy row that
# references an *active* ``beagle_imputed`` call is exactly a row that can block the
# supersession ``UPDATE genotype_calls SET is_active = FALSE`` in
# :func:`_deactivate_prior_imputed_calls`: that UPDATE touches the indexed
# ``is_active`` (``idx_gc_active``), so DuckDB runs it as delete+reinsert of the row,
# which fires the parent-side FK. Every call this import supersedes is an active
# imputed call when the gate runs (before the import transaction), so this count is
# a superset of the blockers — non-zero means the pre-clear must fire (finding-032).
_IMPUTED_REFERENCING_DISCREPANCIES_COUNT_SQL: Final[str] = """
SELECT COUNT(*)
  FROM discrepancies
 WHERE call_a_id IN (
        SELECT call_id FROM genotype_calls
         WHERE source = 'beagle_imputed'::source_enum AND is_active)
    OR call_b_id IN (
        SELECT call_id FROM genotype_calls
         WHERE source = 'beagle_imputed'::source_enum AND is_active)
"""


def _preclear_discrepancies_for_supersession(conn: DuckDBPyConnection) -> int:
    """Clear ``discrepancies`` before the import TX so supersession is FK-safe.

    :func:`_deactivate_prior_imputed_calls` flips ``genotype_calls.is_active`` to
    FALSE on the prior imputed calls. ``is_active`` is indexed (``idx_gc_active``),
    so DuckDB runs that UPDATE as delete+reinsert of each row, firing the
    parent-side FK from ``discrepancies(call_a_id / call_b_id)`` ->
    ``genotype_calls(call_id)`` — the only table that FK-references
    ``genotype_calls``. DuckDB's FK enforcement reads *pre-transaction* state, so
    any referencing ``discrepancies`` rows must be gone as of a prior **committed**
    transaction; an in-transaction delete is invisible to the check. This is the
    same TX0 split :mod:`genome.annotate.canonicalize` and
    :mod:`genome.annotate.strand_collapse` use for the identical quirk
    (finding-020, finding-032).

    Gated on the presence of >= 1 discrepancy referencing an active
    ``beagle_imputed`` call. When present we ``DELETE FROM discrepancies``
    wholesale, matching both TX0 precedents and the ``collapse-duplicate-variants``
    / ``merge`` steps that immediately follow in the reload runbook and rebuild it
    from the active calls. A first import (or any state with no imputed-referencing
    discrepancy) is a no-op, so additive ingests and the chip-only state are left
    untouched. Returns the number of ``discrepancies`` rows deleted.

    Runs in its **own committed transaction**, before the caller opens the import
    transaction. A crash after this commit (but before / within the import) leaves
    ``discrepancies`` empty with ``genotype_calls`` otherwise intact — a
    re-mergeable state, since ``merge`` rebuilds ``discrepancies`` + consensus.
    """
    gate = conn.execute(_IMPUTED_REFERENCING_DISCREPANCIES_COUNT_SQL).fetchone()
    referencing = int(gate[0]) if gate is not None and gate[0] is not None else 0
    if referencing == 0:
        return 0
    total = conn.execute("SELECT COUNT(*) FROM discrepancies").fetchone()
    total_n = int(total[0]) if total is not None and total[0] is not None else 0
    # Committed transaction (TX0) BEFORE the caller's import transaction: the
    # referencing discrepancies must be gone as of a prior commit for DuckDB's
    # pre-transaction FK check to clear the per-batch ``is_active`` flip. The
    # explicit BEGIN also fails loudly if this is ever mis-placed inside an open
    # transaction (DuckDB forbids a nested BEGIN), instead of silently re-breaking.
    conn.execute("BEGIN TRANSACTION")
    conn.execute("DELETE FROM discrepancies")
    conn.execute("COMMIT")
    logger.info(
        "imputation.import.discrepancies_precleared",
        referencing_active_imputed=referencing,
        total_cleared=total_n,
    )
    return total_n


def _insert_imputed_calls(
    conn: DuckDBPyConnection,
    *,
    base_call_id: int,
    run_id: int,
    imputation_panel: str,
) -> None:
    conn.execute(
        """
        INSERT INTO genotype_calls (
            call_id, variant_id, source, source_chip_version, ingestion_run_id,
            genotype_raw, allele_1, allele_2, is_no_call,
            is_imputed, imputation_r2, imputation_panel,
            raw_strand, strand_status, quality_flags, is_active
        )
        SELECT
            ? + s.ord                          AS call_id,
            vm.variant_id                      AS variant_id,
            'beagle_imputed'::source_enum      AS source,
            NULL                               AS source_chip_version,
            ?                                  AS ingestion_run_id,
            CASE WHEN s.is_no_call THEN './.'
                 ELSE COALESCE(s.allele_1, '') || '/' || COALESCE(s.allele_2, '')
            END                                AS genotype_raw,
            s.allele_1                         AS allele_1,
            s.allele_2                         AS allele_2,
            s.is_no_call                       AS is_no_call,
            TRUE                               AS is_imputed,
            s.imputation_r2                    AS imputation_r2,
            ?                                  AS imputation_panel,
            '+'                                AS raw_strand,
            'resolved_plus'::strand_status_enum AS strand_status,
            s.quality_flags                    AS quality_flags,
            TRUE                               AS is_active
          FROM _impute_stage s
          JOIN variants_master vm
            ON vm.chrom = s.chrom::chromosome_enum
           AND vm.pos_grch38 = s.pos_grch38
           AND vm.ref_allele = s.ref_allele
           AND vm.alt_allele = s.alt_allele
        """,
        [base_call_id, run_id, imputation_panel],
    )


def _next_id(conn: DuckDBPyConnection, table: str, column: str) -> int:
    sql = f"SELECT COALESCE(MAX({column}), 0) FROM {table}"  # noqa: S608
    row = conn.execute(sql).fetchone()
    return int(row[0]) + 1 if row else 1


def _rollup_qc_status(
    call_rate: float,
    sex_inferred: str,
) -> tuple[Literal["pass", "warn", "fail"], str]:
    """Status rollup for imputed QC.

    Imputed call rate should always be ~1.0. Anything below 0.99 is suspicious
    enough to flag as a warning; below 0.95 is a fail. Sex check uses imputed
    X/Y — typically more reliable than chip-only since the imputed panel
    fills missing positions.
    """
    notes: list[str] = []
    status: Literal["pass", "warn", "fail"]
    if call_rate >= 0.99:  # noqa: PLR2004 — explicit threshold for clarity
        status = "pass"
    elif call_rate >= 0.95:  # noqa: PLR2004
        status = "warn"
        notes.append(f"imputed call_rate={call_rate:.4f} below 0.99 pass threshold")
    else:
        status = "fail"
        notes.append(f"imputed call_rate={call_rate:.4f} below 0.95 warn threshold")
    if sex_inferred == "ambiguous":
        notes.append("sex inference ambiguous from imputed X / Y data")
    return status, "; ".join(notes)


def _infer_sex(x_het_rate: float | None, y_called: int) -> Literal["M", "F", "ambiguous"]:
    """Imputed sex inference.

    Reuses the cutoffs from :mod:`genome.ingest.qc` so the inference is
    consistent across sources. The Y-call threshold is the same — imputed Y
    panels are similarly sparse to the chip, so this remains a useful test.
    """
    if y_called >= 5 and (x_het_rate is None or x_het_rate <= 0.05):  # noqa: PLR2004
        return "M"
    if y_called < 5 and (x_het_rate is not None and x_het_rate >= 0.10):  # noqa: PLR2004
        return "F"
    return "ambiguous"


def _write_sample_qc(
    conn: DuckDBPyConnection,
    *,
    run_id: int,
    counters: _ImportCounters,
    mean_r2: float | None,
    low_r2_count: int,
) -> int:
    """Write the imputed sample's QC row and return its qc_id."""
    total = counters.variants_total
    call_rate = counters.variants_called / total if total else 0.0
    het_rate = (
        counters.autosomal_het / counters.autosomal_called if counters.autosomal_called else 0.0
    )
    x_het_rate = counters.x_het / counters.x_called if counters.x_called else None
    sex = _infer_sex(x_het_rate, counters.y_called)
    status, notes = _rollup_qc_status(call_rate, sex)

    qc_id = _next_id(conn, "sample_qc", "qc_id")
    conn.execute(
        """
        INSERT INTO sample_qc (
            qc_id, run_id,
            call_rate, heterozygosity_rate, het_outlier,
            sex_inferred, chr_x_het_rate,
            mean_imputation_r2, low_r2_count,
            qc_status, qc_notes
        )
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        [
            qc_id,
            run_id,
            Decimal(f"{call_rate:.4f}"),
            Decimal(f"{het_rate:.4f}"),
            sex,
            Decimal(f"{x_het_rate:.4f}") if x_het_rate is not None else None,
            mean_r2,
            low_r2_count,
            status,
            notes or None,
        ],
    )
    return qc_id


def _process_one_vcf(  # noqa: PLR0913 — per-VCF parameters mirror the writer's `write_calls` shape
    conn: DuckDBPyConnection,
    *,
    path: Path,
    chrom: str,
    counters: _ImportCounters,
    run_id: int,
    superseded_reason: str,
    imputation_panel: str,
    profile_sex: str | None,
    r2_threshold: float,
    dconf_threshold: float,
    batch_size: int,
) -> tuple[int, int]:
    """Stream one VCF into the DB. Returns ``(new_master_rows, deactivated_calls)``."""
    new_master_total = 0
    deactivated_total = 0
    for batch in _stream_chromosome(
        path,
        chrom,
        counters,
        profile_sex=profile_sex,
        r2_threshold=r2_threshold,
        dconf_threshold=dconf_threshold,
        batch_size=batch_size,
    ):
        _create_stage_table(conn)
        _stage_batch(conn, batch)
        new_master_total += _upsert_variants_master(conn)
        deactivated_total += _deactivate_prior_imputed_calls(
            conn,
            superseded_reason=superseded_reason,
        )
        base_call_id = _next_id(conn, "genotype_calls", "call_id")
        _insert_imputed_calls(
            conn,
            base_call_id=base_call_id,
            run_id=run_id,
            imputation_panel=imputation_panel,
        )
        _refresh_imputed_flag(conn)
        conn.execute("DROP TABLE IF EXISTS _impute_stage")
    return new_master_total, deactivated_total


def _resolve_result_vcfs(
    archive: ImputationArchive,
    explicit_paths: tuple[Path, ...] | None,
) -> list[tuple[str, Path]]:
    """Build the per-chromosome list of ``(chrom, path)`` to ingest.

    If ``explicit_paths`` is supplied (tests / non-standard layouts), the
    chromosome is inferred from the filename's leading ``chr<N>``. Otherwise
    we walk the archive's ``result/`` directory.
    """
    paths = list(explicit_paths) if explicit_paths is not None else archive.list_result_vcfs()
    out: list[tuple[str, Path]] = []
    for p in paths:
        name = p.name
        if not name.lower().startswith("chr"):
            continue
        # Pull the chromosome token: "chr1.dose.vcf.gz" -> "1"; "chrX..." -> "X".
        rest = name[3:]
        chrom = rest.split(".", 1)[0].upper()
        if chrom in _IMPUTABLE_CHROMS:
            out.append((chrom, p))
    return out


def parse_chromosomes_filter(raw: str | None) -> frozenset[str] | None:
    """Parse a ``--chromosomes`` CLI value into a canonical chromosome set.

    Accepts a comma-separated list like ``"1,2,X"``. Empty / whitespace tokens
    are ignored. Every token must resolve to a valid imputable chromosome
    label or :class:`ValueError` is raised so the user gets immediate
    feedback. Returns ``None`` when ``raw`` is ``None`` (no filter requested).
    """
    if raw is None:
        return None
    tokens = [t.strip().upper().removeprefix("CHR") for t in raw.split(",") if t.strip()]
    if not tokens:
        msg = "chromosome filter is empty after parsing; pass at least one chromosome"
        raise ValueError(msg)
    bad = [t for t in tokens if t not in _IMPUTABLE_CHROMS]
    if bad:
        msg = (
            f"invalid chromosome(s) {sorted(set(bad))!r}; "
            f"valid imputable chromosomes are {sorted(_IMPUTABLE_CHROMS)}"
        )
        raise ValueError(msg)
    return frozenset(tokens)


def _apply_chromosomes_filter(
    vcf_inputs: list[tuple[str, Path]],
    chromosomes: frozenset[str] | None,
) -> list[tuple[str, Path]]:
    """Drop ``(chrom, path)`` pairs not in ``chromosomes``. No-op when ``None``."""
    if chromosomes is None:
        return vcf_inputs
    return [(c, p) for c, p in vcf_inputs if c in chromosomes]


def _count_chromosome_variants(
    path: Path,
    chrom: str,
    *,
    profile_sex: str | None,
    r2_threshold: float,
    dconf_threshold: float,
) -> tuple[int, int]:
    """Count ``(kept, dropped)`` variants for one chromosome's VCF.

    Used by the dry-run path. Routes every variant through the same
    :func:`_variant_quality` decision as the real import (chromosome match,
    biallelic SNV, then the region/sex-aware quality gate) but writes nothing, so
    the dry-run count can never diverge from what the import would keep.
    """
    kept = 0
    dropped = 0
    reader = _open_imputed_vcf(path)
    try:
        for v in reader:
            mapped = _normalize_chrom_label(str(v.CHROM))
            if mapped != chrom:
                continue
            alts = tuple(str(a) for a in v.ALT or [])
            if not _is_biallelic_snv(str(v.REF), alts):
                continue
            verdict = _variant_quality(
                chrom=chrom,
                pos=int(v.POS),
                profile_sex=profile_sex,
                r2=_extract_r2(v.INFO),
                ds=_extract_ds(v) if chrom == "X" else None,
                is_imputed=_extract_imp(v) if chrom == "X" else False,
                r2_threshold=r2_threshold,
                dconf_threshold=dconf_threshold,
            )
            if verdict.keep:
                kept += 1
            else:
                dropped += 1
    finally:
        reader.close()
    return kept, dropped


def _run_dry_run(
    imputation_id: int,
    vcf_inputs: list[tuple[str, Path]],
    *,
    profile_sex: str | None,
    r2_threshold: float,
    dconf_threshold: float,
) -> DryRunResult:
    """Parse each VCF without writing to the DB. Returns the planned summary."""
    log = logger.bind(imputation_id=imputation_id, n_vcfs=len(vcf_inputs))
    log.info(
        "imputation.import.dry_run.start",
        r2_threshold=r2_threshold,
        dconf_threshold=dconf_threshold,
        profile_sex=profile_sex,
    )
    per_chrom: dict[str, int] = {}
    total = 0
    dropped_total = 0
    for chrom, path in vcf_inputs:
        kept, dropped = _count_chromosome_variants(
            path,
            chrom,
            profile_sex=profile_sex,
            r2_threshold=r2_threshold,
            dconf_threshold=dconf_threshold,
        )
        per_chrom[chrom] = kept
        total += kept
        dropped_total += dropped
        log.info(
            "imputation.import.dry_run.chrom",
            chrom=chrom,
            variants_kept=kept,
            variants_below_threshold=dropped,
        )
    estimated_seconds = (
        total / _ESTIMATED_VARIANTS_PER_SECOND if _ESTIMATED_VARIANTS_PER_SECOND else 0.0
    )
    log.info(
        "imputation.import.dry_run.complete",
        variants_total=total,
        variants_below_threshold=dropped_total,
        estimated_seconds=estimated_seconds,
    )
    return DryRunResult(
        imputation_id=imputation_id,
        chromosomes_planned=tuple(c for c, _ in vcf_inputs),
        variants_total=total,
        variants_below_threshold=dropped_total,
        per_chrom=per_chrom,
        r2_threshold=r2_threshold,
        dconf_threshold=dconf_threshold,
        profile_sex=profile_sex,
        estimated_seconds=estimated_seconds,
    )


def _guard_already_imported(run: ImputationRun, *, force_reimport: bool) -> None:
    """Raise if ``run`` has been imported before and the user didn't pass ``--force-reimport``.

    "Already imported" is detected by ``variants_output`` being non-NULL on
    the run row — that field is populated by :func:`record_import_volumes` at
    the end of a successful import, so its presence is the persistent marker
    that an import has run against this id at least once.
    """
    if force_reimport:
        return
    if run.variants_output is None:
        return
    msg = (
        f"Run {run.imputation_id} has already been imported. Use "
        f"`--force-reimport` to start over, or specify `--chromosomes` to "
        f"import additional chromosomes."
    )
    raise RuntimeError(msg)


@dataclass(frozen=True, slots=True)
class _ImportPlan:
    """Resolved import inputs after validation, chromosome filtering, and run lookup."""

    run: ImputationRun
    archive: ImputationArchive
    vcf_inputs: list[tuple[str, Path]]


def _validate_import_options(
    *, r2_threshold: float, dconf_threshold: float, batch_size: int
) -> None:
    if not 0.0 <= r2_threshold <= 1.0:
        msg = f"r2_threshold must be between 0.0 and 1.0, got {r2_threshold!r}"
        raise ValueError(msg)
    if not 0.5 <= dconf_threshold <= 1.0:  # noqa: PLR2004 — dconf = max(DS,1-DS) ∈ [0.5, 1]
        msg = f"dconf_threshold must be between 0.5 and 1.0, got {dconf_threshold!r}"
        raise ValueError(msg)
    if batch_size <= 0:
        msg = f"batch_size must be positive, got {batch_size!r}"
        raise ValueError(msg)


def _plan_import(  # noqa: PLR0913 — option set comes from the public API surface
    imputation_id: int,
    *,
    duckdb_path: Path,
    archive_root: Path,
    explicit_vcf_paths: tuple[Path, ...] | None,
    chromosomes: frozenset[str] | None,
    dry_run: bool,
    force_reimport: bool,
) -> _ImportPlan:
    """Resolve the run row, archive layout, and per-chromosome VCF list."""
    with duckdb_connection(duckdb_path) as conn:
        run = fetch_run(conn, imputation_id)
        if run is None:
            msg = f"imputation_id {imputation_id} not found"
            raise ValueError(msg)
    _validate_for_import(run)
    if not dry_run:
        _guard_already_imported(run, force_reimport=force_reimport)

    archive = ImputationArchive.for_run(archive_root, imputation_id)
    vcf_inputs = _resolve_result_vcfs(archive, explicit_vcf_paths)
    if not vcf_inputs:
        msg = (
            f"no per-chromosome VCFs found under {archive.result_dir}. "
            "Run `genome imputation run <id>` first; the runbook walks through the steps."
        )
        raise RuntimeError(msg)

    vcf_inputs = _apply_chromosomes_filter(vcf_inputs, chromosomes)
    if not vcf_inputs:
        msg = (
            f"chromosome filter {sorted(chromosomes) if chromosomes else '-'} "
            f"left no matching VCFs under {archive.result_dir}."
        )
        raise RuntimeError(msg)

    if chromosomes is not None:
        logger.info(
            "imputation.import.chromosomes_filter",
            imputation_id=imputation_id,
            chromosomes=sorted(chromosomes),
        )
    return _ImportPlan(run=run, archive=archive, vcf_inputs=vcf_inputs)


def _load_manifest(archive: ImputationArchive) -> dict[str, object] | None:
    """Parse the prepare ``upload/MANIFEST.json`` into a dict, or ``None``.

    Returns ``None`` when the manifest is absent or unparseable — the
    ``explicit_vcf_paths`` / non-standard-layout case (e.g. the test fixtures and
    pre-PR-5a archives) where there is no prepare manifest to trust.
    """
    try:
        raw = archive.upload_manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _load_manifest_variants_per_chrom(archive: ImputationArchive) -> dict[str, int] | None:
    """Read ``variants_per_chrom`` from the prepare ``MANIFEST.json``, or ``None``.

    Returns ``None`` (guard disabled) when the manifest is absent or unparseable.
    A present, well-formed manifest yields the per-chromosome *upload* counts the
    empty-output guard compares against. Keys are the unprefixed chromosome labels
    the prepare step writes (``"X"``, ``"1"``), matching the import loop's
    ``chrom``.
    """
    payload = _load_manifest(archive)
    if payload is None:
        return None
    per_chrom = payload.get("variants_per_chrom")
    if not isinstance(per_chrom, dict):
        return None
    out: dict[str, int] = {}
    for key, value in per_chrom.items():
        with contextlib.suppress(TypeError, ValueError):
            out[str(key)] = int(value)
    return out


def _manifest_profile_sex(archive: ImputationArchive) -> str | None:
    """Return the prepare-time ``profile_sex`` (``'M'`` / ``'F'``) or ``None``.

    The manifest records the sex the prepare step used to render the chrX target
    (PR 5a). ``'ambiguous'``, a missing key, or no manifest all map to ``None`` —
    the importer then uses the DR2 gate everywhere, the pre-PR-5a behavior.
    """
    payload = _load_manifest(archive)
    if payload is None:
        return None
    value = payload.get("profile_sex")
    return value if value in {"M", "F"} else None


def _manifest_chrx_ploidy(archive: ImputationArchive) -> str | None:
    """Return the prepare-time ``chrx_ploidy`` decision, or ``None``.

    ``'male_nonpar_haploid'`` means the prepare step rendered the male non-PAR
    target haploid, so its Beagle output is dosage-confidence territory (DR2 is
    dead there). That is the signal the importer's fail-closed sex guard keys on.
    """
    payload = _load_manifest(archive)
    if payload is None:
        return None
    value = payload.get("chrx_ploidy")
    return str(value) if isinstance(value, str) else None


def _normalize_sex_override(explicit: str | None) -> str | None:
    """Normalize a caller-supplied sex override to ``'M'`` / ``'F'`` / ``None``.

    ``None`` (and the string ``'auto'``, case-insensitive) mean "resolve from the
    manifest". Anything else must be ``'M'`` / ``'F'`` or this raises so a typo
    can't silently disable the male non-PAR gate.
    """
    if explicit is None:
        return None
    value = explicit.strip()
    if value.lower() == "auto":
        return None
    upper = value.upper()
    if upper in {"M", "F"}:
        return upper
    msg = f"sex override must be 'M', 'F', or 'auto'; got {explicit!r}"
    raise ValueError(msg)


def _resolve_import_profile_sex(archive: ImputationArchive, explicit: str | None) -> str | None:
    """Resolve the profile sex the import gate branches on (PR 5a).

    An explicit ``'M'`` / ``'F'`` override wins; otherwise fall back to the
    manifest's prepare-time ``profile_sex``. ``None`` means "no determinate male
    profile" → the DR2 gate is used for every chromosome (the pre-PR-5a path).
    """
    override = _normalize_sex_override(explicit)
    if override is not None:
        return override
    return _manifest_profile_sex(archive)


def _guard_male_chrx_sex(
    archive: ImputationArchive,
    *,
    profile_sex: str | None,
    chrx_in_scope: bool,
) -> None:
    """Fail closed when male-scoped chrX output can't be gated as male (PR 5a).

    When the prepare manifest says the chrX target was rendered
    ``male_nonpar_haploid`` and chrX is in this import's scope, the non-PAR Beagle
    output is on the dead-DR2 / live-dosage-confidence regime (finding-031). If
    the resolved ``profile_sex`` is not ``'M'``, the importer would fall back to
    the DR2 gate and silently drop **all** non-PAR — the acute regression this PR
    exists to fix. Refuse instead, pointing the user at ``--sex M``.
    """
    if not chrx_in_scope:
        return
    if _manifest_chrx_ploidy(archive) != "male_nonpar_haploid":
        return
    if profile_sex == "M":
        return
    msg = (
        "the prepare manifest rendered the chrX target as 'male_nonpar_haploid', "
        "but the profile sex did not resolve to 'M' "
        f"(resolved {profile_sex!r}). Importing male non-PAR chrX under the DR2 "
        "gate would drop every non-PAR call (DR2 is structurally 0 there, "
        "finding-031). Pass --sex M to import it, or --chromosomes to exclude chrX."
    )
    raise RuntimeError(msg)


def _assert_chrom_not_silently_empty(
    *,
    chrom: str,
    raw_records: int,
    manifest_per_chrom: dict[str, int] | None,
) -> None:
    """Refuse a cleanly-closed-yet-empty imputed result for a non-trivial input.

    finding-008 #2 can leave a result VCF that cyvcf2 reads as zero variants
    without raising. :func:`_assert_result_vcf_intact` catches the *truncated*
    (missing-BGZF-EOF) shape; this guard catches the *cleanly-closed-empty*
    shape by cross-checking the prepare manifest: if Beagle was handed >0
    variants for ``chrom`` but its output streamed none, the run failed
    silently. Skipped when no manifest is available (no trusted input count).
    An all-below-R²-threshold chromosome is unaffected — it still streamed
    ``raw_records > 0`` before the threshold dropped them.

    For chrX under M3-physical (PR 5a) this guard keeps the whole-chromosome
    backstop, but the *primary* per-region empty check now lives in the runner
    (:func:`genome.imputation.beagle_runner._impute_chrx_regions`): a region
    handed >0 chip inputs that imputes zero records fails there, *before* the
    three regions are concatenated into the single ``result/chrX.vcf.gz`` this
    function sees. The manifest's ``variants_per_chrom["X"]`` here is the sum
    across regions, so a fully-empty chrX concat is still caught.
    """
    if manifest_per_chrom is None:
        return
    expected = manifest_per_chrom.get(chrom, 0)
    if raw_records == 0 and expected > 0:
        msg = (
            f"imputed result for chr{chrom} streamed zero variant records, but the "
            f"prepare manifest recorded {expected} uploaded variant(s) for it — the "
            f"Beagle run for this chromosome almost certainly failed (finding-008). "
            f"Re-run `genome imputation run <id>`, or import the intact chromosomes "
            f"with `--chromosomes` (excluding chr{chrom})."
        )
        raise RuntimeError(msg)


def _execute_import(  # noqa: PLR0913 — options pass through directly to the writers
    imputation_id: int,
    plan: _ImportPlan,
    *,
    duckdb_path: Path,
    imputation_panel: str,
    profile_sex: str | None,
    r2_threshold: float,
    dconf_threshold: float,
    batch_size: int,
) -> ImportResult:
    """Run the per-chromosome ingest transaction. Caller owns plan creation."""
    log = logger.bind(
        imputation_id=imputation_id,
        n_vcfs=len(plan.vcf_inputs),
        profile_sex=profile_sex,
        r2_threshold=r2_threshold,
        dconf_threshold=dconf_threshold,
        batch_size=batch_size,
    )
    log.info("imputation.import.start")
    counters = _ImportCounters()

    with duckdb_connection(duckdb_path) as conn:
        # Pre-clear discrepancies that FK-reference active imputed calls, in a
        # committed transaction *before* the import transaction opens. The per-batch
        # supersession (``genotype_calls.is_active = FALSE``) delete+reinserts those
        # rows (``is_active`` is indexed), firing the parent-side ``discrepancies``
        # FK; DuckDB's FK check reads pre-transaction state, so the referencing rows
        # must already be committed-away (finding-032; the same TX0 split
        # canonicalize / strand_collapse use). ``merge`` rebuilds ``discrepancies``,
        # and the reload always re-merges after import, so this is safe.
        discrepancies_precleared = _preclear_discrepancies_for_supersession(conn)
        conn.execute("BEGIN TRANSACTION")
        try:
            run_id = insert_ingestion_run(
                conn,
                source="beagle_imputed",
                chip_version=None,
                file_path=str(plan.archive.result_dir),
                file_hash_sha256=(plan.run.output_file_hash_sha256 or ""),
                file_size_bytes=0,  # archive on disk; size not material here
                file_native_build="GRCh38",
                pipeline_version=IMPUTATION_PIPELINE_VERSION,
                variants_total=0,  # backfilled below via UPDATE
                variants_called=0,
                variants_no_call=0,
                variants_imputed=0,
            )
            manifest_per_chrom = _load_manifest_variants_per_chrom(plan.archive)
            if manifest_per_chrom is None:
                log.info("imputation.import.empty_guard.skipped_no_manifest")
            new_master_total = 0
            deactivated_total = 0
            for chrom, path in plan.vcf_inputs:
                new_master, deactivated = _process_one_vcf(
                    conn,
                    path=path,
                    chrom=chrom,
                    counters=counters,
                    run_id=run_id,
                    superseded_reason=f"superseded by imputation_id {imputation_id}",
                    imputation_panel=imputation_panel,
                    profile_sex=profile_sex,
                    r2_threshold=r2_threshold,
                    dconf_threshold=dconf_threshold,
                    batch_size=batch_size,
                )
                _assert_chrom_not_silently_empty(
                    chrom=chrom,
                    raw_records=counters.raw_per_chrom.get(chrom, 0),
                    manifest_per_chrom=manifest_per_chrom,
                )
                new_master_total += new_master
                deactivated_total += deactivated

            mean_r2 = counters.r2_sum / counters.r2_count if counters.r2_count else None

            conn.execute(
                """
                UPDATE ingestion_runs
                   SET variants_total = ?,
                       variants_called = ?,
                       variants_no_call = ?,
                       variants_imputed = ?,
                       completed_at = CURRENT_TIMESTAMP
                 WHERE run_id = ?
                """,
                [
                    counters.variants_total,
                    counters.variants_called,
                    counters.variants_no_call,
                    counters.variants_total,
                    run_id,
                ],
            )

            low_r2 = counters.r2_count - counters.variants_above_r2_0_3
            qc_id = _write_sample_qc(
                conn,
                run_id=run_id,
                counters=counters,
                mean_r2=mean_r2,
                low_r2_count=max(low_r2, 0),
            )
            record_import_volumes(
                conn,
                imputation_id,
                variants_output=counters.variants_total,
                mean_r2=mean_r2,
                variants_above_r2_0_3=counters.variants_above_r2_0_3,
                variants_above_r2_0_8=counters.variants_above_r2_0_8,
                r2_threshold=r2_threshold,
            )
            # Every transition to ``completed`` stamps ``completed_at``.
            # The Beagle runner stamps this when the run finishes
            # imputation; the import step re-stamps idempotently here so
            # an import that flips a still-``processing`` run to
            # ``completed`` doesn't leave the timestamp NULL.
            update_status(conn, imputation_id, status="completed", set_completed=True)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            log.exception("imputation.import.failed")
            raise

    log.info(
        "imputation.import.complete",
        variants_total=counters.variants_total,
        variants_below_threshold=counters.variants_below_threshold,
        nonpar_confident=counters.nonpar_confident,
        new_master_rows=new_master_total,
        deactivated_prior_calls=deactivated_total,
        discrepancies_precleared=discrepancies_precleared,
        mean_r2=mean_r2,
    )
    return ImportResult(
        imputation_id=imputation_id,
        ingestion_run_id=run_id,
        qc_id=qc_id,
        variants_total=counters.variants_total,
        variants_called=counters.variants_called,
        variants_no_call=counters.variants_no_call,
        variants_below_threshold=counters.variants_below_threshold,
        new_variants_master_rows=new_master_total,
        deactivated_prior_calls=deactivated_total,
        mean_r2=mean_r2,
        variants_above_r2_0_3=counters.variants_above_r2_0_3,
        variants_above_r2_0_8=counters.variants_above_r2_0_8,
        r2_threshold=r2_threshold,
        dconf_threshold=dconf_threshold,
        nonpar_confident=counters.nonpar_confident,
        profile_sex=profile_sex,
        chromosomes_imported=tuple(c for c, _ in plan.vcf_inputs),
    )


def import_result(  # noqa: PLR0913 — operational flags map 1:1 to schema/CLI controls
    imputation_id: int,
    *,
    duckdb_path: Path | None = None,
    archive_root: Path | None = None,
    explicit_vcf_paths: tuple[Path, ...] | None = None,
    imputation_panel: str = "1000g_phase3_grch38",
    r2_threshold: float = DEFAULT_R2_THRESHOLD,
    dconf_threshold: float = DEFAULT_DCONF_THRESHOLD,
    chromosomes: frozenset[str] | None = None,
    sex: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    force_reimport: bool = False,
) -> ImportResult | DryRunResult:
    """Stream the imputed VCFs from ``run_<id>/result/`` into the database.

    Idempotence: re-running on a run that's already in ``status='completed'``
    with imputed calls will deactivate the prior calls and write a fresh
    ``ingestion_runs`` row. The user gets a no-op-on-content (same rows
    re-inserted) plus new supersession rows for audit.

    ``explicit_vcf_paths`` overrides the archive layout — used by tests and
    by users whose result directory differs from the default.

    Operational flags:

    * ``r2_threshold`` (default ``0.3``): variants whose imputation R²
      (``INFO/DR2``, falling back to ``R2``/``Rsq``) is below ``r2_threshold``
      are skipped and never written to ``genotype_calls``. The threshold is
      recorded on ``imputation_runs.r2_threshold``. Applies everywhere except
      the male non-PAR chrX dosage-confidence regime below.
    * ``dconf_threshold`` (default ``0.9``): for **male non-PAR chrX** the DR2 is
      structurally dead (finding-031), so the gate switches to dosage-confidence
      ``max(DS, 1 - DS)`` and keeps variants ``>= dconf_threshold``. The kept
      dosage-confidence is stored into ``imputation_r2`` with a
      ``'nonpar_dosage_conf'`` ``quality_flags`` marker (the documented overload);
      these rows stay out of the DR2 run-stats.
    * ``sex``: ``'M'`` / ``'F'`` override (or ``None`` / ``'auto'`` to resolve from
      the prepare manifest's ``profile_sex``). Selects whether male non-PAR chrX
      uses the dosage-confidence gate. A manifest that rendered the chrX target
      ``male_nonpar_haploid`` fails closed unless this resolves to ``'M'``.
    * ``chromosomes``: optional set of chromosome labels (e.g. ``{"1","X"}``);
      when set, only matching files are processed.
    * ``batch_size`` (default ``50_000``): rows per Arrow Table bulk-insert.
    * ``dry_run``: parse VCFs and report expected counts / time without
      writing anything. Returns :class:`DryRunResult` instead of
      :class:`ImportResult`.
    * ``force_reimport``: required to re-run import against an id whose
      ``variants_output`` is already populated (i.e. a prior import landed).
      Re-runs use the same supersession-over-update semantics that were
      already in place.
    """
    settings = get_settings()
    db_path = duckdb_path or settings.genome_duckdb_path
    archive_root = archive_root or settings.archive_path

    _validate_import_options(
        r2_threshold=r2_threshold,
        dconf_threshold=dconf_threshold,
        batch_size=batch_size,
    )
    plan = _plan_import(
        imputation_id,
        duckdb_path=db_path,
        archive_root=archive_root,
        explicit_vcf_paths=explicit_vcf_paths,
        chromosomes=chromosomes,
        dry_run=dry_run,
        force_reimport=force_reimport,
    )
    # Resolve the profile sex the chrX gate branches on, then fail closed if the
    # chrX output was rendered male-haploid but the sex did not resolve to 'M'
    # (importing it under the DR2 gate would zero non-PAR — finding-031). Both
    # the dry-run and the real import share this so the dry-run count matches.
    profile_sex = _resolve_import_profile_sex(plan.archive, sex)
    chrx_in_scope = any(chrom == "X" for chrom, _ in plan.vcf_inputs)
    _guard_male_chrx_sex(plan.archive, profile_sex=profile_sex, chrx_in_scope=chrx_in_scope)

    if dry_run:
        return _run_dry_run(
            imputation_id,
            plan.vcf_inputs,
            profile_sex=profile_sex,
            r2_threshold=r2_threshold,
            dconf_threshold=dconf_threshold,
        )

    return _execute_import(
        imputation_id,
        plan,
        duckdb_path=db_path,
        imputation_panel=imputation_panel,
        profile_sex=profile_sex,
        r2_threshold=r2_threshold,
        dconf_threshold=dconf_threshold,
        batch_size=batch_size,
    )


def _validate_for_import(run: ImputationRun) -> None:
    """Confirm a run is in a state where importing makes sense.

    Raises ``RuntimeError`` for ``pending`` (not yet downloaded) or ``failed``;
    accepts ``processing`` (the user might be importing a partially-recovered
    result, which is valid) and ``completed`` (the normal path).
    """
    if run.status not in {"processing", "completed"}:
        msg = (
            f"imputation_id {run.imputation_id} is in status {run.status!r}; "
            f"download the result first (status must be 'completed' before import)"
        )
        raise RuntimeError(msg)
