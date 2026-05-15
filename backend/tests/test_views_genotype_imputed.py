"""Semantic tests for the imputed columns in ``platform_coverage_v`` and
``call_comparison_v``.

The Phase 4 pivot from TopMed to local Beagle (``finding-006``) added the
``'beagle_imputed'`` value to ``source_enum`` and made it the only
imputation source that writes real data. The retained ``'topmed_imputed'``
enum value is kept for backward compatibility but has no production code
path that writes it.

These tests pin the views' filter expressions to ``'beagle_imputed'`` so a
future regression that flips them back to ``'topmed_imputed'`` (or that
broadens to an OR including the dead enum value) is caught by the suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def _insert_variant(  # noqa: PLR0913 — schema-aligned positional fields
    conn: DuckDBPyConnection,
    *,
    variant_id: int,
    rsid: str,
    chrom: str,
    pos: int,
    ref: str,
    alt: str,
) -> None:
    conn.execute(
        """
        INSERT INTO variants_master (
            variant_id, rsid, chrom, pos_grch38, pos_grch37,
            ref_allele, alt_allele, variant_type,
            has_genotyped_call, has_imputed_call, is_acmg_sf,
            liftover_chain, liftover_status
        ) VALUES (?, ?, ?::chromosome_enum, ?, ?, ?, ?, 'SNV',
                  FALSE, TRUE, FALSE, 'native_grch38', 'native_grch38')
        """,
        [variant_id, rsid, chrom, pos, pos, ref, alt],
    )


def _insert_ingestion_run(conn: DuckDBPyConnection, run_id: int, source: str) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_runs (
            run_id, source, source_chip_version, file_path, file_hash_sha256,
            file_size_bytes, file_native_build,
            variants_total, variants_called, variants_no_call, variants_imputed,
            status, pipeline_version, completed_at
        ) VALUES (?, ?::source_enum, 'test', ?, ?, 100, 'GRCh38',
                  10, 10, 0, 0, 'completed', 'pipeline_test', CURRENT_TIMESTAMP)
        """,
        [run_id, source, f"/test/run_{run_id}", "0" * 64],
    )


def _insert_call(  # noqa: PLR0913 — schema-aligned positional fields
    conn: DuckDBPyConnection,
    *,
    call_id: int,
    variant_id: int,
    source: str,
    run_id: int,
    allele_1: str,
    allele_2: str,
    imputation_r2: float | None,
) -> None:
    conn.execute(
        """
        INSERT INTO genotype_calls (
            call_id, variant_id, source, source_chip_version, ingestion_run_id,
            genotype_raw, allele_1, allele_2, is_no_call,
            is_imputed, imputation_r2, imputation_panel,
            raw_strand, strand_status, quality_flags, is_active
        ) VALUES (?, ?, ?::source_enum, 'test', ?,
                  ?, ?, ?, FALSE,
                  TRUE, ?, '1000g_phase3_grch38',
                  '+', 'resolved_plus'::strand_status_enum,
                  ARRAY[]::VARCHAR[], TRUE)
        """,
        [
            call_id,
            variant_id,
            source,
            run_id,
            allele_1 + allele_2,
            allele_1,
            allele_2,
            imputation_r2,
        ],
    )


def test_imputed_view_columns_filter_on_beagle_imputed(
    isolated_settings: dict[str, str],
) -> None:
    """A ``beagle_imputed`` call surfaces in the imputed view columns; a
    ``topmed_imputed`` call (the retained-but-unused enum value) does not.
    """
    init_databases()
    duckdb_path = Path(isolated_settings["GENOME_DUCKDB_PATH"])

    beagle_variant_id = 1
    topmed_variant_id = 2
    beagle_r2 = 0.91

    with duckdb_connection(duckdb_path) as conn:
        _insert_ingestion_run(conn, run_id=1, source="beagle_imputed")
        _insert_ingestion_run(conn, run_id=2, source="topmed_imputed")
        _insert_variant(
            conn,
            variant_id=beagle_variant_id,
            rsid="rs_beagle",
            chrom="1",
            pos=1000,
            ref="A",
            alt="G",
        )
        _insert_variant(
            conn,
            variant_id=topmed_variant_id,
            rsid="rs_topmed",
            chrom="1",
            pos=2000,
            ref="C",
            alt="T",
        )
        _insert_call(
            conn,
            call_id=1,
            variant_id=beagle_variant_id,
            source="beagle_imputed",
            run_id=1,
            allele_1="A",
            allele_2="G",
            imputation_r2=beagle_r2,
        )
        _insert_call(
            conn,
            call_id=2,
            variant_id=topmed_variant_id,
            source="topmed_imputed",
            run_id=2,
            allele_1="C",
            allele_2="T",
            imputation_r2=0.77,
        )

    # Positive case: the Beagle-imputed variant should land in the imputed
    # columns of both views.
    with duckdb_connection(duckdb_path, read_only=True) as conn:
        beagle_coverage = conn.execute(
            "SELECT in_23andme, in_ancestry, in_imputed"
            " FROM platform_coverage_v WHERE variant_id = ?",
            [beagle_variant_id],
        ).fetchone()
        beagle_comparison = conn.execute(
            "SELECT gt_imputed, imputed_r2 FROM call_comparison_v WHERE variant_id = ?",
            [beagle_variant_id],
        ).fetchone()

        topmed_coverage = conn.execute(
            "SELECT in_23andme, in_ancestry, in_imputed"
            " FROM platform_coverage_v WHERE variant_id = ?",
            [topmed_variant_id],
        ).fetchone()
        topmed_comparison = conn.execute(
            "SELECT gt_imputed, imputed_r2 FROM call_comparison_v WHERE variant_id = ?",
            [topmed_variant_id],
        ).fetchone()

    assert beagle_coverage is not None
    assert beagle_coverage == (False, False, True)
    assert beagle_comparison is not None
    assert beagle_comparison[0] == "A/G"
    assert beagle_comparison[1] == beagle_r2

    # Negative case: the retained-but-unused 'topmed_imputed' enum value
    # must NOT surface in the imputed view columns. The filters are
    # 'beagle_imputed'-only by design (finding-006).
    assert topmed_coverage is not None
    assert topmed_coverage == (False, False, False)
    assert topmed_comparison is not None
    assert topmed_comparison[0] is None
    assert topmed_comparison[1] is None
