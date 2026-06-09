"""Tests for :mod:`genome.imputation.rsid_cleanup` — the synthetic-rsid sweep.

The sweep NULLs Beagle's synthetic ``chrom:pos:ref:alt`` strings left in
``variants_master.rsid`` by Phase-4 imputation ingest (finding-021), while leaving
real ``rs#``, chip-internal ``i####``, and vendor chip-probe IDs (``kgp…`` /
``VGXS…`` / ``acom_…``) untouched. Those probe IDs sit in the non-``rs`` / non-``i``
/ non-``.`` complement but are not synthetic, so the sweep logs them as a leftover
rather than aborting. A genotype_calls child FK-referencing the synthetic row
exercises the DuckDB delete+reinsert parent-FK quirk the index drop/rebuild guards
against.
"""

from __future__ import annotations

import pytest
import structlog
from structlog.testing import capture_logs

from genome.db import duckdb_connection, init_databases
from genome.imputation.rsid_cleanup import normalize_imputed_rsids


@pytest.fixture(autouse=True)
def _reset_structlog_after_each_test():
    """Restore structlog defaults so capture_logs doesn't leak between tests."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def _seed_variants() -> None:
    """Seed a mixed rsid population plus an FK child on a synthetic row.

    Two coordinate-format synthetics (a bare ``chrom:…`` and a ``chr``-prefixed one),
    a real ``rs#``, a chip-internal ``i####``, a NULL, and three chip-probe IDs
    (``kgp…`` / ``VGXS…`` / ``acom_rs…``) that the regex must leave untouched — the
    miniature of the real 16-row complement gap on which the old equality guard
    aborted.
    """
    with duckdb_connection() as conn:
        conn.execute(
            """
            INSERT INTO variants_master
                (variant_id, rsid, chrom, pos_grch38, ref_allele, alt_allele)
            VALUES
                (1, '14:29619977:C:T',   '14', 29619977, 'C', 'T'),
                (2, 'rs7412',            '19', 44908822, 'C', 'T'),
                (3, 'i3000001',          '1',  1000,     'A', 'G'),
                (4, NULL,                '2',  2000,     'A', 'G'),
                (5, 'kgp1851883',        '3',  3000,     'A', 'G'),
                (6, 'VGXS34713',         '4',  4000,     'A', 'G'),
                (7, 'acom_rs201205097',  '5',  5000,     'A', 'G'),
                (8, 'chr7:55259515:T:G', '7',  55259515, 'T', 'G')
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
    assert cleaned == 2

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

    # Only the two coordinate-format synthetics are NULLed.
    assert rows[1] is None  # synthetic chr:pos:ref:alt -> NULL
    assert rows[8] is None  # chr-prefixed synthetic -> NULL
    # Everything else is preserved untouched, including the chip-probe IDs that
    # the old equality guard mistook for an over/under-match and aborted on.
    assert rows[2] == "rs7412"  # real rs# untouched
    assert rows[3] == "i3000001"  # chip-internal i#### untouched
    assert rows[4] is None  # already-NULL untouched
    assert rows[5] == "kgp1851883"  # Illumina 1000G probe untouched (the gap)
    assert rows[6] == "VGXS34713"  # vendor probe untouched
    assert rows[7] == "acom_rs201205097"  # Ancestry probe (embeds rs) untouched
    assert child == (1,)  # FK child intact across the delete+reinsert
    assert index_present == 1  # idx_vm_rsid rebuilt in the finally


def test_normalize_is_idempotent(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths to a temp root
) -> None:
    init_databases()
    _seed_variants()

    with duckdb_connection() as conn:
        first = normalize_imputed_rsids(conn)
        # Second run: matched == 0, so the function returns early. The chip-probe
        # leftover is permanent residue, so the preflight log MUST still fire here —
        # this is the only run that proves the leftover log sits *before* the
        # `if matched == 0: return 0` early return. A log placed after the return
        # would vanish on exactly this steady-state path.
        with capture_logs() as captured:
            second = normalize_imputed_rsids(conn)
    assert first == 2
    assert second == 0

    preflight = [c for c in captured if c["event"] == "imputation.normalize_rsids.preflight"]
    assert len(preflight) == 1  # would be 0 if the leftover log sat after `return 0`
    assert preflight[0]["matched"] == 0
    assert preflight[0]["leftover"] == 3
    assert set(preflight[0]["leftover_sample"]) == {
        "kgp1851883",
        "VGXS34713",
        "acom_rs201205097",
    }
