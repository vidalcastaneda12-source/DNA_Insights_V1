"""Persist normalized calls + run metadata into DuckDB.

The writer assumes it owns the connection for the duration of an ingest. All
writes happen inside one transaction so a partial failure leaves the database
clean.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pyarrow as pa

if TYPE_CHECKING:
    from collections.abc import Iterable

    from duckdb import DuckDBPyConnection

    from genome.ingest.models import NormalizedCall, Source
    from genome.ingest.qc import SampleQC


def _next_id(conn: DuckDBPyConnection, table: str, column: str) -> int:
    # ``table`` / ``column`` are module-internal literals (writer only — never
    # user input). The S608 lint is a false positive here.
    sql = f"SELECT COALESCE(MAX({column}), 0) FROM {table}"  # noqa: S608
    row = conn.execute(sql).fetchone()
    return int(row[0]) + 1 if row is not None else 1


def insert_ingestion_run(  # noqa: PLR0913 — schema fields are not collapsible
    conn: DuckDBPyConnection,
    *,
    source: Source,
    chip_version: str | None,
    file_path: str,
    file_hash_sha256: str,
    file_size_bytes: int,
    file_native_build: str,
    pipeline_version: str,
    variants_total: int,
    variants_called: int,
    variants_no_call: int,
    variants_imputed: int,
    variants_dropped_non_canonical: int = 0,
    variants_dropped_lift_to_non_canonical: int = 0,
    status: Literal["completed", "failed"] = "completed",
    error_log: str | None = None,
) -> int:
    """Insert an ``ingestion_runs`` row with final counts and return its ``run_id``.

    All counts and the final status are written at insert time. DuckDB rejects
    UPDATEs on rows that already have inbound FK references (here ``sample_qc``
    and ``genotype_calls`` reference ``run_id``), so this is the simpler
    correctness-preserving shape: a single insert per run, inside the
    pipeline's outer transaction. On error the surrounding transaction rolls
    back and the row vanishes with everything else.
    """
    run_id = _next_id(conn, "ingestion_runs", "run_id")
    conn.execute(
        """
        INSERT INTO ingestion_runs (
            run_id, source, source_chip_version, file_path, file_hash_sha256,
            file_size_bytes, file_native_build,
            variants_total, variants_called, variants_no_call, variants_imputed,
            variants_dropped_non_canonical,
            variants_dropped_lift_to_non_canonical,
            status, error_log, pipeline_version, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [
            run_id,
            source,
            chip_version,
            file_path,
            file_hash_sha256,
            file_size_bytes,
            file_native_build,
            variants_total,
            variants_called,
            variants_no_call,
            variants_imputed,
            variants_dropped_non_canonical,
            variants_dropped_lift_to_non_canonical,
            status,
            error_log,
            pipeline_version,
        ],
    )
    return run_id


def _stage_calls(
    conn: DuckDBPyConnection,
    calls: list[NormalizedCall],
) -> None:
    """Materialize the call batch into a temp table for set-based joins.

    The batch is loaded as a single PyArrow Table via ``conn.register()``;
    DuckDB then streams it columnar into ``_ingest_stage`` in one
    ``INSERT ... SELECT``. ``executemany`` re-prepares per row in DuckDB and
    runs in minutes at whole-array (~600K-row) scale; the columnar Arrow path
    runs in well under a second.
    """
    conn.execute("DROP TABLE IF EXISTS _ingest_stage")
    conn.execute(
        """
        CREATE TEMP TABLE _ingest_stage (
            ord            BIGINT,
            rsid           VARCHAR,
            chrom          VARCHAR,
            pos_grch38     BIGINT,
            pos_grch37     BIGINT,
            ref_allele     VARCHAR,
            alt_allele     VARCHAR,
            variant_type   VARCHAR,
            allele_1       VARCHAR,
            allele_2       VARCHAR,
            is_no_call     BOOLEAN,
            strand_status  VARCHAR,
            liftover_chain VARCHAR,
            liftover_status VARCHAR,
            quality_flags  VARCHAR[]
        )
        """,
    )
    if not calls:
        return

    arrays = [
        pa.array(range(len(calls)), type=pa.int64()),
        pa.array([c.rsid for c in calls], type=pa.string()),
        pa.array([c.chrom for c in calls], type=pa.string()),
        pa.array([c.pos_grch38 for c in calls], type=pa.int64()),
        pa.array([c.pos_grch37 for c in calls], type=pa.int64()),
        pa.array([c.ref_allele for c in calls], type=pa.string()),
        pa.array([c.alt_allele for c in calls], type=pa.string()),
        pa.array([c.variant_type for c in calls], type=pa.string()),
        pa.array([c.allele_1 or None for c in calls], type=pa.string()),
        pa.array([c.allele_2 or None for c in calls], type=pa.string()),
        pa.array([c.is_no_call for c in calls], type=pa.bool_()),
        pa.array([c.strand_status for c in calls], type=pa.string()),
        pa.array([c.liftover_chain for c in calls], type=pa.string()),
        pa.array([c.liftover_status for c in calls], type=pa.string()),
        pa.array(
            [list(c.quality_flags) for c in calls],
            type=pa.list_(pa.string()),
        ),
    ]
    names = [
        "ord",
        "rsid",
        "chrom",
        "pos_grch38",
        "pos_grch37",
        "ref_allele",
        "alt_allele",
        "variant_type",
        "allele_1",
        "allele_2",
        "is_no_call",
        "strand_status",
        "liftover_chain",
        "liftover_status",
        "quality_flags",
    ]
    table = pa.Table.from_arrays(arrays, names=names)

    conn.register("_ingest_stage_arrow", table)
    try:
        conn.execute("INSERT INTO _ingest_stage SELECT * FROM _ingest_stage_arrow")
    finally:
        conn.unregister("_ingest_stage_arrow")


def _upsert_variants_master(conn: DuckDBPyConnection) -> int:
    """Insert variants from ``_ingest_stage`` that don't yet exist; return new-row count."""
    before = conn.execute("SELECT COUNT(*) FROM variants_master").fetchone()
    before_n = int(before[0]) if before is not None else 0
    conn.execute(
        """
        INSERT INTO variants_master (
            rsid, chrom, pos_grch38, pos_grch37,
            ref_allele, alt_allele, variant_type,
            liftover_chain, liftover_status
        )
        SELECT
            ANY_VALUE(s.rsid),
            s.chrom::chromosome_enum,
            s.pos_grch38,
            ANY_VALUE(s.pos_grch37),
            s.ref_allele,
            s.alt_allele,
            ANY_VALUE(s.variant_type)::variant_type_enum,
            ANY_VALUE(s.liftover_chain),
            ANY_VALUE(s.liftover_status)
          FROM _ingest_stage s
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
    after_n = int(after[0]) if after is not None else 0
    return after_n - before_n


def _deactivate_prior_calls(
    conn: DuckDBPyConnection,
    *,
    source: Source,
    superseded_reason: str,
) -> int:
    """Mark prior active calls for this (variant, source) inactive; return count."""
    res = conn.execute(
        """
        UPDATE genotype_calls
           SET is_active = FALSE,
               superseded_reason = ?
         WHERE is_active = TRUE
           AND source = ?::source_enum
           AND variant_id IN (
                SELECT vm.variant_id
                  FROM _ingest_stage s
                  JOIN variants_master vm
                    ON vm.chrom = s.chrom::chromosome_enum
                   AND vm.pos_grch38 = s.pos_grch38
                   AND vm.ref_allele = s.ref_allele
                   AND vm.alt_allele = s.alt_allele
           )
        """,
        [superseded_reason, source],
    )
    # DuckDB's UPDATE returns the count via fetchone() on most builds; defensive.
    row = res.fetchone() if hasattr(res, "fetchone") else None
    if row is None or row[0] is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _insert_genotype_calls(
    conn: DuckDBPyConnection,
    *,
    run_id: int,
    source: Source,
    source_chip_version: str | None,
    base_call_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO genotype_calls (
            call_id, variant_id, source, source_chip_version, ingestion_run_id,
            genotype_raw, allele_1, allele_2, is_no_call,
            is_imputed, raw_strand, strand_status, quality_flags, is_active
        )
        SELECT
            ? + s.ord                         AS call_id,
            vm.variant_id                     AS variant_id,
            ?::source_enum                    AS source,
            ?                                 AS source_chip_version,
            ?                                 AS ingestion_run_id,
            CASE WHEN s.is_no_call THEN '--'
                 ELSE COALESCE(s.allele_1, '') || COALESCE(s.allele_2, '')
            END                               AS genotype_raw,
            s.allele_1                        AS allele_1,
            s.allele_2                        AS allele_2,
            s.is_no_call                      AS is_no_call,
            FALSE                             AS is_imputed,
            '+'                               AS raw_strand,
            s.strand_status::strand_status_enum AS strand_status,
            s.quality_flags                   AS quality_flags,
            TRUE                              AS is_active
          FROM _ingest_stage s
          JOIN variants_master vm
            ON vm.chrom = s.chrom::chromosome_enum
           AND vm.pos_grch38 = s.pos_grch38
           AND vm.ref_allele = s.ref_allele
           AND vm.alt_allele = s.alt_allele
        """,
        [base_call_id, source, source_chip_version, run_id],
    )


def _refresh_master_flags(conn: DuckDBPyConnection) -> None:
    """Update ``has_genotyped_call`` / ``has_imputed_call`` for variants in this batch."""
    conn.execute(
        """
        UPDATE variants_master
           SET has_genotyped_call = TRUE
         WHERE variant_id IN (
                SELECT vm.variant_id
                  FROM _ingest_stage s
                  JOIN variants_master vm
                    ON vm.chrom = s.chrom::chromosome_enum
                   AND vm.pos_grch38 = s.pos_grch38
                   AND vm.ref_allele = s.ref_allele
                   AND vm.alt_allele = s.alt_allele
           )
        """,
    )


def write_calls(
    conn: DuckDBPyConnection,
    calls: Iterable[NormalizedCall],
    *,
    run_id: int,
    source: Source,
    source_chip_version: str | None,
) -> tuple[int, int]:
    """Stage, dedup, deactivate prior, then insert all calls in this batch.

    Returns ``(new_variants_master_rows, deactivated_prior_calls)``.
    """
    materialized = list(calls)
    _stage_calls(conn, materialized)
    new_variants = _upsert_variants_master(conn)
    deactivated = _deactivate_prior_calls(
        conn,
        source=source,
        superseded_reason=f"superseded by run {run_id}",
    )
    base_call_id = _next_id(conn, "genotype_calls", "call_id")
    _insert_genotype_calls(
        conn,
        run_id=run_id,
        source=source,
        source_chip_version=source_chip_version,
        base_call_id=base_call_id,
    )
    _refresh_master_flags(conn)
    conn.execute("DROP TABLE IF EXISTS _ingest_stage")
    return new_variants, deactivated


def insert_sample_qc(
    conn: DuckDBPyConnection,
    *,
    run_id: int,
    qc: SampleQC,
    mean_imputation_r2: float | None = None,
    low_r2_count: int | None = None,
) -> int:
    """Insert a ``sample_qc`` row and return its ``qc_id``."""
    qc_id = _next_id(conn, "sample_qc", "qc_id")
    conn.execute(
        """
        INSERT INTO sample_qc (
            qc_id, run_id,
            call_rate, heterozygosity_rate, het_outlier,
            sex_inferred, sex_expected, sex_check_passed, chr_x_het_rate,
            mean_imputation_r2, low_r2_count,
            qc_status, qc_notes
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
        """,
        [
            qc_id,
            run_id,
            qc.call_rate,
            qc.heterozygosity_rate,
            qc.het_outlier,
            qc.sex_inferred,
            qc.chr_x_het_rate,
            mean_imputation_r2,
            low_r2_count,
            qc.qc_status,
            qc.qc_notes or None,
        ],
    )
    return qc_id
