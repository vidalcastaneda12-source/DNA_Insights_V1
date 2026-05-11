"""Writer-level tests for the ingest stage.

The end-to-end pipeline tests in ``test_ingest_pipeline.py`` cover output
equivalence against fixture files. This module isolates the writer's hot path
(``_stage_calls``) so future regressions on the bulk-load path are caught
cheaply without spinning up the full pipeline.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import duckdb

from genome.ingest.models import NormalizedCall
from genome.ingest.writer import _stage_calls

if TYPE_CHECKING:
    from pathlib import Path


def _synthetic_call(i: int) -> NormalizedCall:
    """A deterministic ``NormalizedCall`` for benchmark loads.

    Spread positions/chromosomes lightly so the staging pass exercises realistic
    column cardinality rather than a single repeated value.
    """
    chrom = str((i % 22) + 1)
    a1, a2 = ("A", "G") if i % 2 == 0 else ("C", "T")
    return NormalizedCall(
        rsid=f"rs{i}",
        chrom=chrom,
        pos_grch38=1_000 + i,
        pos_grch37=1_000 + i,
        ref_allele="A" if i % 2 == 0 else "C",
        alt_allele="G" if i % 2 == 0 else "T",
        variant_type="SNV",
        allele_1=a1,
        allele_2=a2,
        is_no_call=False,
        strand_status="resolved_plus",
        liftover_chain="native_grch38",
        liftover_status="native_grch38",
        quality_flags=(),
    )


def test_stage_calls_handles_empty_input(tmp_path: Path) -> None:
    """Empty call list still creates the temp table with zero rows."""
    conn = duckdb.connect(database=str(tmp_path / "empty.duckdb"))
    try:
        _stage_calls(conn, [])
        (n,) = conn.execute("SELECT COUNT(*) FROM _ingest_stage").fetchone()
    finally:
        conn.close()
    assert n == 0


def test_stage_calls_preserves_ord_and_columns(tmp_path: Path) -> None:
    """Round-trip a small batch and verify column values + ``ord`` sequence.

    ``ord`` is the only column the downstream ``_insert_genotype_calls`` SELECT
    references directly (`? + s.ord AS call_id`), so its correctness is part of
    the writer's contract — not just a leftover staging artifact.
    """
    calls = [_synthetic_call(i) for i in range(5)]
    # One row with empty alleles to verify the `"" -> NULL` mapping is preserved.
    calls.append(
        NormalizedCall(
            rsid=None,
            chrom="X",
            pos_grch38=42,
            pos_grch37=None,
            ref_allele="A",
            alt_allele="T",
            variant_type="SNV",
            allele_1="",
            allele_2="",
            is_no_call=True,
            strand_status="ambiguous_palindrome",
            liftover_chain=None,
            liftover_status="lift_failed",
            quality_flags=("palindrome",),
        ),
    )

    conn = duckdb.connect(database=str(tmp_path / "roundtrip.duckdb"))
    try:
        _stage_calls(conn, calls)
        rows = conn.execute(
            "SELECT ord, rsid, chrom, allele_1, allele_2, quality_flags"
            " FROM _ingest_stage ORDER BY ord",
        ).fetchall()
    finally:
        conn.close()

    assert [r[0] for r in rows] == [0, 1, 2, 3, 4, 5]
    assert rows[0][1] == "rs0"
    assert rows[5][1] is None  # rsid None preserved
    assert rows[5][2] == "X"
    # Empty alleles round-trip as NULL, matching the prior implementation.
    assert rows[5][3] is None
    assert rows[5][4] is None
    assert rows[5][5] == ["palindrome"]


def test_stage_calls_100k_rows_under_two_seconds(tmp_path: Path) -> None:
    """Bulk-load benchmark: 100K synthetic calls must stage in < 2 seconds.

    Guards against regressing the PyArrow ``INSERT ... SELECT`` path back to the
    per-row ``executemany`` shape, which took tens of minutes for 631K rows.
    """
    n = 100_000
    calls = [_synthetic_call(i) for i in range(n)]

    conn = duckdb.connect(database=str(tmp_path / "bench.duckdb"))
    try:
        start = time.perf_counter()
        _stage_calls(conn, calls)
        elapsed = time.perf_counter() - start
        (staged,) = conn.execute("SELECT COUNT(*) FROM _ingest_stage").fetchone()
    finally:
        conn.close()

    assert staged == n
    assert elapsed < 2.0, f"_stage_calls took {elapsed:.2f}s for {n} rows (budget 2s)"
