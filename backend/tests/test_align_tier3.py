"""Tests for :mod:`genome.annotate.align_tier3`.

Covers the post-merge tier-3 consensus alignment: seed two variants_master
rows at the same (chrom, pos) carrying complementary alleles (mimicking the
strand-flip case under Scope A canonicalization), only one matching a dbSNP
record; assert that ``align_tier3_consensus`` deletes the non-canonical-side
``consensus_genotypes`` row while leaving the canonical side intact.

The contributing_calls invariant is also asserted: the canonical-side
consensus's ``contributing_calls`` array is untouched and still references
both call_ids — no information is lost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from genome.annotate.align_tier3 import (
    AlignResult,
    DbsnpNotLoadedError,
    align_tier3_consensus,
)
from genome.annotate.source_versions import insert_source_version
from genome.annotate.supersession import flip_to_new_version
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from duckdb import DuckDBPyConnection


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    try:
        yield
    finally:
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Seeding helpers (local to keep the file self-contained — siblings duplicate
# the same shape in test_canonicalize.py / test_annotate_index_refresh.py).
# ---------------------------------------------------------------------------


def _seed_dbsnp_version(conn: DuckDBPyConnection) -> int:
    svid = insert_source_version(
        conn,
        source_db="dbsnp",
        version="157",
        source_url=None,
        source_file_hash="d" * 64,
        source_file_size=1,
        record_count=0,
    )
    flip_to_new_version(conn, source="dbsnp", table="dbsnp_annotations", new_source_version_id=svid)
    return svid


def _seed_variant(  # noqa: PLR0913 — identity fields not collapsible
    conn: DuckDBPyConnection,
    variant_id: int,
    *,
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alt: str = "G",
) -> None:
    conn.execute(
        """
        INSERT INTO variants_master
            (variant_id, chrom, pos_grch38, ref_allele, alt_allele, variant_type)
        VALUES (?, ?::chromosome_enum, ?, ?, ?, 'SNV'::variant_type_enum)
        """,
        [variant_id, chrom, pos, ref, alt],
    )


def _seed_disagreement_resolved_consensus(
    conn: DuckDBPyConnection,
    variant_id: int,
    *,
    contributing_calls: Sequence[int],
) -> None:
    """Insert a consensus_genotypes row mimicking merge's tier-3 strand-flip output."""
    conn.execute(
        """
        INSERT INTO consensus_genotypes
            (variant_id, consensus_allele_1, consensus_allele_2, is_no_call,
             dosage, consensus_method, contributing_calls, resolution_rule)
        VALUES (?, 'A', 'G', FALSE, 1,
                'disagreement_resolved'::consensus_method_enum, ?, 'consensus_v1')
        """,
        [variant_id, list(contributing_calls)],
    )


def _seed_dbsnp_annotation(  # noqa: PLR0913 — dbsnp identity fields not collapsible
    conn: DuckDBPyConnection,
    dbsnp_id: int,
    svid: int,
    *,
    rsid: str = "rs1",
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alts: Sequence[str] = ("G",),
) -> None:
    conn.execute(
        """
        INSERT INTO dbsnp_annotations
            (dbsnp_id, rsid, chrom, pos_grch38, ref_allele, alt_alleles,
             variant_class, source_version_id, retrieval_date)
        VALUES (?, ?, ?::chromosome_enum, ?, ?, ?, 'snv', ?, CURRENT_TIMESTAMP)
        """,
        [dbsnp_id, rsid, chrom, pos, ref, list(alts), svid],
    )


def _consensus_variant_ids(conn: DuckDBPyConnection) -> list[int]:
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT variant_id FROM consensus_genotypes ORDER BY variant_id",
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def test_raises_when_no_dbsnp_loaded(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with duckdb_connection() as conn, pytest.raises(DbsnpNotLoadedError):
        align_tier3_consensus(conn)


# ---------------------------------------------------------------------------
# Core alignment
# ---------------------------------------------------------------------------


def test_deletes_non_canonical_side_consensus(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Two same-pos rows with disagreement_resolved consensus; only one matches
    dbSNP -> the other's consensus row is deleted.

    Mimics the post-merge state of a Scope-A canonicalized strand-flip pair:
    variant 1 = (A,G) matches dbSNP (ref=A alt=G); variant 2 = (C,T) is the
    complement-only sibling (no dbSNP match). After alignment, only variant
    1's consensus survives; variant 2's consensus is deleted (variant 2 stays
    in variants_master as a vestigial row).
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")  # canonical orientation
        _seed_variant(conn, 2, ref="C", alt="T")  # complement-only sibling
        _seed_disagreement_resolved_consensus(conn, 1, contributing_calls=[10, 20])
        _seed_disagreement_resolved_consensus(conn, 2, contributing_calls=[10, 20])
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))

        result = align_tier3_consensus(conn)
        survivors = _consensus_variant_ids(conn)
        # The canonical-side consensus is untouched: same contributing_calls,
        # same disagreement_resolved method.
        canonical = conn.execute(
            """
            SELECT consensus_method, contributing_calls
              FROM consensus_genotypes WHERE variant_id = 1
            """,
        ).fetchone()
        # variants_master still has both rows — only consensus_genotypes is
        # touched by this command (variant 2 stays as a vestigial row).
        vm_count = conn.execute("SELECT COUNT(*) FROM variants_master").fetchone()

    assert isinstance(result, AlignResult)
    assert result.pairs_examined == 1
    assert result.rows_deleted == 1
    assert survivors == [1]
    assert canonical is not None
    assert canonical[0] == "disagreement_resolved"
    assert list(canonical[1]) == [10, 20]
    assert vm_count is not None
    assert vm_count[0] == 2


def test_no_op_when_both_sides_canonical(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """If both same-pos rows match dbSNP somehow (rare; e.g. both alts present
    as dbSNP alt_alleles entries), the bucket is left alone — we don't have
    grounds to delete either side.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_variant(conn, 2, ref="A", alt="C")
        _seed_disagreement_resolved_consensus(conn, 1, contributing_calls=[10, 20])
        _seed_disagreement_resolved_consensus(conn, 2, contributing_calls=[10, 20])
        # dbSNP has both G and C as alts at this position.
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G", "C"))

        result = align_tier3_consensus(conn)
        survivors = _consensus_variant_ids(conn)

    assert result.pairs_examined == 0
    assert result.rows_deleted == 0
    assert survivors == [1, 2]


def test_no_op_when_both_sides_non_canonical(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """If neither row matches dbSNP (no clean canonical side), the bucket is
    left alone — we don't pick a winner arbitrarily.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_variant(conn, 2, ref="C", alt="T")
        _seed_disagreement_resolved_consensus(conn, 1, contributing_calls=[10, 20])
        _seed_disagreement_resolved_consensus(conn, 2, contributing_calls=[10, 20])
        # dbSNP at a different position — no match for either row.
        _seed_dbsnp_annotation(conn, 1, svid, chrom="1", pos=9999, ref="A", alts=("G",))

        result = align_tier3_consensus(conn)
        survivors = _consensus_variant_ids(conn)

    assert result.pairs_examined == 0
    assert result.rows_deleted == 0
    assert survivors == [1, 2]


def test_idempotent_second_run(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A second run on already-aligned data finds no actionable pairs."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_variant(conn, 2, ref="C", alt="T")
        _seed_disagreement_resolved_consensus(conn, 1, contributing_calls=[10, 20])
        _seed_disagreement_resolved_consensus(conn, 2, contributing_calls=[10, 20])
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))

        first = align_tier3_consensus(conn)
        assert first.rows_deleted == 1

        second = align_tier3_consensus(conn)
    assert second.pairs_examined == 0
    assert second.rows_deleted == 0


def test_ignores_non_disagreement_resolved_consensus(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Same-pos rows whose consensus_method is NOT 'disagreement_resolved' are
    irrelevant to this command (they're not a tier-3 strand-flip pair).
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_variant(conn, 2, ref="C", alt="T")
        # Both consensus rows are 'both_concordant' — not strand-flip cases.
        conn.execute(
            """
            INSERT INTO consensus_genotypes
                (variant_id, consensus_method, resolution_rule)
            VALUES (1, 'both_concordant'::consensus_method_enum, 'consensus_v1'),
                   (2, 'both_concordant'::consensus_method_enum, 'consensus_v1')
            """,
        )
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))

        result = align_tier3_consensus(conn)
        survivors = _consensus_variant_ids(conn)

    assert result.pairs_examined == 0
    assert result.rows_deleted == 0
    assert survivors == [1, 2]
