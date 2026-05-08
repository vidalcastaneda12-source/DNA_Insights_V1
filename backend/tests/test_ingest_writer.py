"""Writer-stage unit and benchmark coverage.

The pipeline-level tests in :mod:`test_ingest_pipeline` exercise the writer
end-to-end against the schema. This module covers the staging step in
isolation: shape preservation across the PyArrow bulk-load path, and a
throughput benchmark guarding the regression that brought the user's 631K-row
ingest down from ~14 minutes to <1 second of staging.
"""

from __future__ import annotations

import time

import duckdb

from genome.ingest.models import NormalizedCall
from genome.ingest.writer import _stage_calls


def _make_call(i: int) -> NormalizedCall:
    """Construct one synthetic NormalizedCall covering every staged column.

    Cycles through nullable shapes (``rsid``, ``pos_grch37``, ``allele_*``,
    ``liftover_chain``, ``quality_flags``) so the bulk-load round-trip is
    exercised on the same null/non-null mix the real pipeline emits.
    """
    is_no_call = i % 50 == 0
    has_rsid = i % 3 != 0
    has_grch37 = i % 4 != 0
    return NormalizedCall(
        rsid=f"rs{i}" if has_rsid else None,
        chrom=str((i % 22) + 1),
        pos_grch38=10_000 + i,
        pos_grch37=(10_000 + i) if has_grch37 else None,
        ref_allele="A",
        alt_allele="G",
        variant_type="SNV",
        allele_1="" if is_no_call else "A",
        allele_2="" if is_no_call else "G",
        is_no_call=is_no_call,
        strand_status="resolved_plus",
        liftover_chain="hg19_to_hg38" if has_grch37 else None,
        liftover_status="lifted_ok" if has_grch37 else "native_grch38",
        quality_flags=("flag_a", "flag_b") if i % 7 == 0 else (),
    )


def test_stage_calls_round_trips_shape_and_nulls() -> None:
    """The PyArrow bulk-load preserves null shape and the empty-allele→None rule.

    The previous ``executemany`` path coerced ``c.allele_1 or None`` into the
    stage row; the Arrow path keeps that branch and we assert the same null
    pattern lands in the temp table.
    """
    conn = duckdb.connect(":memory:")
    calls = [_make_call(i) for i in range(50)]
    _stage_calls(conn, calls)

    n = conn.execute("SELECT COUNT(*) FROM _ingest_stage").fetchone()
    assert n == (50,)

    # Spot-check the first row matches the synthetic generator exactly.
    first = conn.execute(
        "SELECT ord, rsid, chrom, pos_grch38, pos_grch37, ref_allele, alt_allele,"
        " variant_type, allele_1, allele_2, is_no_call, strand_status,"
        " liftover_chain, liftover_status, quality_flags"
        " FROM _ingest_stage ORDER BY ord LIMIT 1",
    ).fetchone()
    assert first == (
        0,
        None,  # i=0 → has_rsid False (i % 3 == 0)
        "1",
        10_000,
        None,  # i=0 → has_grch37 False (i % 4 == 0)
        "A",
        "G",
        "SNV",
        None,  # i=0 → is_no_call True (i % 50 == 0) → empty alleles → None
        None,
        True,
        "resolved_plus",
        None,  # has_grch37 False → no chain
        "native_grch38",
        ["flag_a", "flag_b"],  # i % 7 == 0
    )

    # Empty-allele rows are stored as NULL, not "".
    null_alleles = conn.execute(
        "SELECT COUNT(*) FROM _ingest_stage WHERE is_no_call AND allele_1 IS NULL",
    ).fetchone()
    assert null_alleles == (1,)


def test_stage_calls_handles_empty_input() -> None:
    """An empty calls batch still creates the temp table (zero rows)."""
    conn = duckdb.connect(":memory:")
    _stage_calls(conn, [])
    n = conn.execute("SELECT COUNT(*) FROM _ingest_stage").fetchone()
    assert n == (0,)


def test_stage_calls_drops_and_recreates_on_repeat_invocation() -> None:
    """Two consecutive batches don't accumulate; the temp table is reset."""
    conn = duckdb.connect(":memory:")
    _stage_calls(conn, [_make_call(i) for i in range(10)])
    _stage_calls(conn, [_make_call(i) for i in range(5)])
    n = conn.execute("SELECT COUNT(*) FROM _ingest_stage").fetchone()
    assert n == (5,)


def test_stage_calls_100k_under_2s() -> None:
    """Bulk-load 100K synthetic calls in <2s wall-clock.

    Regression guard for the original ``executemany`` path: that shape took
    ~minutes for 600K rows because DuckDB re-prepares the parameter-bound
    INSERT per row. The PyArrow ``register()`` + ``INSERT ... SELECT`` path
    streams the batch columnar and finishes in well under a second on
    developer-class hardware. The 2s ceiling is a generous regression guard.
    """
    conn = duckdb.connect(":memory:")
    calls = [_make_call(i) for i in range(100_000)]

    start = time.perf_counter()
    _stage_calls(conn, calls)
    elapsed = time.perf_counter() - start

    n = conn.execute("SELECT COUNT(*) FROM _ingest_stage").fetchone()
    assert n == (100_000,)
    assert elapsed < 2.0, f"_stage_calls took {elapsed:.2f}s; budget is 2.0s"
