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
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

import pyarrow as pa
import structlog

from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.imputation._htslib import silence_htslib_contig_warnings
from genome.imputation.archive import ImputationArchive
from genome.imputation.runs import (
    ImputationRun,
    fetch_run,
    record_import_volumes,
    update_status,
)
from genome.ingest.writer import insert_ingestion_run

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
# Empirical: ~30M variants stream in ~30 min on a dev machine
# (the benchmark test confirms 1M rows clear in well under 60s).
# Rate used for the dry-run time estimate.
_ESTIMATED_VARIANTS_PER_SECOND: Final[int] = 16_500

_IMPUTABLE_CHROMS: Final[frozenset[str]] = frozenset(
    {*(str(i) for i in range(1, 23)), "X", "Y"},
)


@dataclass(slots=True)
class _ImportCounters:
    """Mutable accumulators threaded through the streaming ingest."""

    variants_total: int = 0
    variants_called: int = 0
    variants_no_call: int = 0
    variants_above_r2_0_3: int = 0
    variants_above_r2_0_8: int = 0
    variants_below_threshold: int = 0
    r2_sum: float = 0.0
    r2_count: int = 0
    autosomal_called: int = 0
    autosomal_het: int = 0
    x_called: int = 0
    x_het: int = 0
    y_called: int = 0
    per_chrom: dict[str, int] = field(default_factory=dict)


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


def _open_imputed_vcf(path: Path) -> _cyvcf2_typing.VCF:
    """Open ``path`` with cyvcf2. Import is deferred so the type hint stays clean."""
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
    r2: float | None,
) -> None:
    """Append one already-validated variant to the batch and update counters."""
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
    _update_r2_counters(counters, r2)
    counters.per_chrom[chrom] = counters.per_chrom.get(chrom, 0) + 1

    batch.rsid.append(rsid)
    batch.chrom.append(chrom)
    batch.pos.append(pos)
    batch.ref.append(ref)
    batch.alt.append(alt)
    batch.allele_1.append(allele_1)
    batch.allele_2.append(allele_2)
    batch.is_no_call.append(is_no_call)
    batch.imputation_r2.append(r2)


def _stream_chromosome(
    path: Path,
    chrom: str,
    counters: _ImportCounters,
    *,
    r2_threshold: float,
    batch_size: int,
) -> Iterator[_Batch]:
    """Yield batches of normalized rows from one chromosome's imputed VCF.

    Variants whose imputation R² (INFO/DR2 or fallback) falls below
    ``r2_threshold`` are skipped before the batch grows — they don't reach
    ``variants_master`` or ``genotype_calls`` and are accounted for in
    ``counters.variants_below_threshold``. Variants missing an R² value pass
    through (matching the pre-filter behavior; rare on Beagle output but
    defensible for non-Beagle VCFs).
    """
    log = logger.bind(path=str(path), chrom=chrom)
    log.info("imputation.import.chrom.start")
    batch = _Batch()
    # Beagle output is missing canonical ##contig headers, so cyvcf2 prints
    # a per-record contig warning across millions of records. Scope the
    # htslib log-level suppression to this read.
    with silence_htslib_contig_warnings():
        reader = _open_imputed_vcf(path)
        try:
            for v in reader:
                mapped = _normalize_chrom_label(str(v.CHROM))
                if mapped != chrom:
                    # The file's chromosome doesn't match the expected one — skip
                    # silently. Beagle should never produce this, but a misnamed
                    # file would otherwise corrupt the per-chrom counters.
                    continue
                alts = tuple(str(a) for a in v.ALT or [])
                if not _is_biallelic_snv(str(v.REF), alts):
                    continue

                r2 = _extract_r2(v.INFO)
                if r2 is not None and r2 < r2_threshold:
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
                    pos=int(v.POS),
                    rsid=None if not v.ID else str(v.ID),
                    ref=str(v.REF),
                    alt=alts[0],
                    allele_1=allele_1,
                    allele_2=allele_2,
                    is_no_call=is_no_call,
                    r2=r2,
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
            imputation_r2  DOUBLE
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
            raw_strand, strand_status, is_active
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
    r2_threshold: float,
    batch_size: int,
) -> tuple[int, int]:
    """Stream one VCF into the DB. Returns ``(new_master_rows, deactivated_calls)``."""
    new_master_total = 0
    deactivated_total = 0
    for batch in _stream_chromosome(
        path,
        chrom,
        counters,
        r2_threshold=r2_threshold,
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
    r2_threshold: float,
) -> tuple[int, int]:
    """Count ``(kept, dropped)`` variants for one chromosome's VCF.

    Used by the dry-run path. Mirrors ``_stream_chromosome``'s filter rules
    (chromosome match, biallelic SNV, R²-threshold) but writes nothing.
    """
    kept = 0
    dropped = 0
    # Same htslib-warning suppression as the streaming path; Beagle output
    # is missing canonical ##contig headers and would otherwise flood
    # stderr on the dry-run scan.
    with silence_htslib_contig_warnings():
        reader = _open_imputed_vcf(path)
        try:
            for v in reader:
                mapped = _normalize_chrom_label(str(v.CHROM))
                if mapped != chrom:
                    continue
                alts = tuple(str(a) for a in v.ALT or [])
                if not _is_biallelic_snv(str(v.REF), alts):
                    continue
                r2 = _extract_r2(v.INFO)
                if r2 is not None and r2 < r2_threshold:
                    dropped += 1
                    continue
                kept += 1
        finally:
            reader.close()
    return kept, dropped


def _run_dry_run(
    imputation_id: int,
    vcf_inputs: list[tuple[str, Path]],
    *,
    r2_threshold: float,
) -> DryRunResult:
    """Parse each VCF without writing to the DB. Returns the planned summary."""
    log = logger.bind(imputation_id=imputation_id, n_vcfs=len(vcf_inputs))
    log.info("imputation.import.dry_run.start", r2_threshold=r2_threshold)
    per_chrom: dict[str, int] = {}
    total = 0
    dropped_total = 0
    for chrom, path in vcf_inputs:
        kept, dropped = _count_chromosome_variants(path, chrom, r2_threshold=r2_threshold)
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


def _validate_import_options(*, r2_threshold: float, batch_size: int) -> None:
    if not 0.0 <= r2_threshold <= 1.0:
        msg = f"r2_threshold must be between 0.0 and 1.0, got {r2_threshold!r}"
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


def _execute_import(  # noqa: PLR0913 — options pass through directly to the writers
    imputation_id: int,
    plan: _ImportPlan,
    *,
    duckdb_path: Path,
    imputation_panel: str,
    r2_threshold: float,
    batch_size: int,
) -> ImportResult:
    """Run the per-chromosome ingest transaction. Caller owns plan creation."""
    log = logger.bind(
        imputation_id=imputation_id,
        n_vcfs=len(plan.vcf_inputs),
        r2_threshold=r2_threshold,
        batch_size=batch_size,
    )
    log.info("imputation.import.start")
    counters = _ImportCounters()

    with duckdb_connection(duckdb_path) as conn:
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
                    r2_threshold=r2_threshold,
                    batch_size=batch_size,
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
        new_master_rows=new_master_total,
        deactivated_prior_calls=deactivated_total,
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
    chromosomes: frozenset[str] | None = None,
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
      recorded on ``imputation_runs.r2_threshold``.
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

    _validate_import_options(r2_threshold=r2_threshold, batch_size=batch_size)
    plan = _plan_import(
        imputation_id,
        duckdb_path=db_path,
        archive_root=archive_root,
        explicit_vcf_paths=explicit_vcf_paths,
        chromosomes=chromosomes,
        dry_run=dry_run,
        force_reimport=force_reimport,
    )
    if dry_run:
        return _run_dry_run(imputation_id, plan.vcf_inputs, r2_threshold=r2_threshold)

    return _execute_import(
        imputation_id,
        plan,
        duckdb_path=db_path,
        imputation_panel=imputation_panel,
        r2_threshold=r2_threshold,
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
