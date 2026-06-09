"""Tests for :mod:`genome.imputation.rsid_cleanup` — the synthetic-rsid sweep.

The sweep NULLs Beagle's synthetic ``chrom:pos:ref:alt`` strings left in
``variants_master.rsid`` by Phase-4 imputation ingest (finding-021), while
leaving real ``rs#`` and chip-internal ``i####`` IDs untouched. A genotype_calls
child FK-referencing the synthetic row exercises the DuckDB delete+reinsert
parent-FK quirk the index drop/rebuild guards against.
"""

from __future__ import annotations

from genome.db import duckdb_connection, init_databases
from genome.imputation.rsid_cleanup import normalize_imputed_rsids


def _seed_variants() -> None:
    """Seed one row of each rsid kind plus an FK child on the synthetic row."""
    with duckdb_connection() as conn:
        conn.execute(
            """
            INSERT INTO variants_master
                (variant_id, rsid, chrom, pos_grch38, ref_allele, alt_allele)
            VALUES
                (1, '14:29619977:C:T', '14', 29619977, 'C', 'T'),
                (2, 'rs7412',          '19', 44908822, 'C', 'T'),
                (3, 'i3000001',        '1',  1000,      'A', 'G'),
                (4, NULL,              '2',  2000,      'A', 'G')
            """,
        )
        # The indexed rsid on this FK-referenced row is what trips DuckDB's
        # parent-side check on UPDATE unless idx_vm_rsid is dropped first.
        conn.execute(
            """
            INSERT INTO genotype_calls
                (call_id, variant_id, source, ingestion_run_id)
            VALUES (1, 1, 'beagle_imputed', 1)
            """,
        )


def test_normalize_nulls_synthetic_leaves_others(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths to a temp root
) -> None:
    init_databases()
    _seed_variants()

    with duckdb_connection() as conn:
        cleaned = normalize_imputed_rsids(conn)
    assert cleaned == 1

    with duckdb_connection() as conn:
        rows = dict(
            conn.execute(
                "SELECT variant_id, rsid FROM variants_master ORDER BY variant_id",
            ).fetchall(),
        )
        child = conn.execute(
            "SELECT variant_id FROM genotype_calls WHERE call_id = 1",
        ).fetchone()
        index_present = conn.execute(
            "SELECT COUNT(*) FROM duckdb_indexes() WHERE index_name = 'idx_vm_rsid'",
        ).fetchone()[0]

    assert rows[1] is None  # synthetic chr:pos:ref:alt -> NULL
    assert rows[2] == "rs7412"  # real rs# untouched
    assert rows[3] == "i3000001"  # chip-internal i#### untouched
    assert rows[4] is None  # already-NULL untouched
    assert child == (1,)  # FK child intact across the delete+reinsert
    assert index_present == 1  # idx_vm_rsid rebuilt in the finally


def test_normalize_is_idempotent(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths to a temp root
) -> None:
    init_databases()
    _seed_variants()

    with duckdb_connection() as conn:
        first = normalize_imputed_rsids(conn)
        second = normalize_imputed_rsids(conn)
    assert first == 1
    assert second == 0
