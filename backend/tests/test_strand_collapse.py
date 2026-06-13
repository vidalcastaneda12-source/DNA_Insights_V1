"""Tests for :mod:`genome.annotate.strand_collapse` (PR 5b — generalized collapse).

Covers the per-edge identification across all five mechanisms (no-call, swap,
strand-flip, hom opposite/same strand), the size-3 multi-allelic DROP, the
legit-multiallelic / non-revcomp protection, the genotype-mismatch + source-
collision skips, the degenerate (no-survivor) skip, the ``--dry-run`` read-only
path, the row-grain supersession + repoint, the rsID coalesce, downstream-table
clears, idempotence, and the post-collapse re-merge (incl. the PR-5b-pre
chip-no-call dependency that keeps an imputed survivor's genotype).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from genome.annotate.source_versions import insert_source_version
from genome.annotate.strand_collapse import (
    DbsnpNotLoadedError,
    StrandCollapseResult,
    collapse_duplicate_variants,
)
from genome.annotate.supersession import flip_to_new_version
from genome.db import duckdb_connection, init_databases
from genome.merge import merge_all

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
# Seeding helpers
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
    rsid: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO variants_master
            (variant_id, rsid, chrom, pos_grch38, ref_allele, alt_allele, variant_type)
        VALUES (?, ?, ?::chromosome_enum, ?, ?, ?, 'SNV'::variant_type_enum)
        """,
        [variant_id, rsid, chrom, pos, ref, alt],
    )


def _seed_call(  # noqa: PLR0913 — call identity fields not collapsible
    conn: DuckDBPyConnection,
    call_id: int,
    variant_id: int,
    *,
    source: str = "23andme",
    allele_1: str | None = "A",
    allele_2: str | None = "G",
    is_no_call: bool = False,
    is_imputed: bool = False,
    is_active: bool = True,
) -> None:
    """Insert one ``genotype_calls`` row + a stub ``ingestion_runs`` row if needed."""
    existing = conn.execute("SELECT COUNT(*) FROM ingestion_runs WHERE run_id = 1").fetchone()
    if existing is not None and existing[0] == 0:
        conn.execute(
            """
            INSERT INTO ingestion_runs
                (run_id, source, file_path, file_hash_sha256, pipeline_version, status)
            VALUES (1, '23andme'::source_enum, '/tmp/x', 'h', 'test',
                    'completed'::ingestion_status_enum)
            """,
        )
    conn.execute(
        """
        INSERT INTO genotype_calls
            (call_id, variant_id, source, ingestion_run_id,
             allele_1, allele_2, is_no_call, is_imputed, is_active)
        VALUES (?, ?, ?::source_enum, 1, ?, ?, ?, ?, ?)
        """,
        [call_id, variant_id, source, allele_1, allele_2, is_no_call, is_imputed, is_active],
    )


def _seed_nocall(conn: DuckDBPyConnection, call_id: int, variant_id: int, *, source: str) -> None:
    _seed_call(
        conn, call_id, variant_id, source=source, allele_1=None, allele_2=None, is_no_call=True
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


def _seed_discrepancy(conn: DuckDBPyConnection, discrepancy_id: int, variant_id: int) -> None:
    conn.execute(
        """
        INSERT INTO discrepancies
            (discrepancy_id, variant_id, discrepancy_type, severity, source_a, call_a_id)
        VALUES (?, ?, 'genotype_mismatch'::discrepancy_type_enum,
                'major'::severity_enum, '23andme'::source_enum, ?)
        """,
        [discrepancy_id, variant_id, discrepancy_id],
    )


def _seed_consensus(conn: DuckDBPyConnection, variant_id: int) -> None:
    conn.execute(
        """
        INSERT INTO consensus_genotypes (variant_id, consensus_method, resolution_rule)
        VALUES (?, 'both_concordant'::consensus_method_enum, 'consensus_v1')
        """,
        [variant_id],
    )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def _variant_ids(conn: DuckDBPyConnection) -> list[int]:
    rows = conn.execute("SELECT variant_id FROM variants_master ORDER BY variant_id").fetchall()
    return [int(r[0]) for r in rows]


def _dangling_calls(conn: DuckDBPyConnection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
          FROM genotype_calls gc
          LEFT JOIN variants_master vm ON vm.variant_id = gc.variant_id
         WHERE vm.variant_id IS NULL
        """,
    ).fetchone()
    return int(row[0]) if row is not None else -1


def _active_calls_on(conn: DuckDBPyConnection, variant_id: int) -> list[tuple[str, str, str, str]]:
    """Return ``(source, allele_1, allele_2, strand_status)`` for active calls on a variant."""
    return [
        (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
        for r in conn.execute(
            """
            SELECT CAST(source AS VARCHAR), allele_1, allele_2, CAST(strand_status AS VARCHAR)
              FROM genotype_calls
             WHERE variant_id = ? AND is_active
             ORDER BY call_id
            """,
            [variant_id],
        ).fetchall()
    ]


def _rsid_of(conn: DuckDBPyConnection, variant_id: int) -> str | None:
    row = conn.execute(
        "SELECT rsid FROM variants_master WHERE variant_id = ?",
        [variant_id],
    ).fetchone()
    assert row is not None
    return None if row[0] is None else str(row[0])


def _variant_id_of_call(conn: DuckDBPyConnection, call_id: int) -> int:
    row = conn.execute(
        "SELECT variant_id FROM genotype_calls WHERE call_id = ?",
        [call_id],
    ).fetchone()
    assert row is not None
    return int(row[0])


def _seed_strandflip_pair(
    conn: DuckDBPyConnection, *, survivor_rsid: str | None = None, dead_rsid: str | None = None
) -> int:
    """Survivor (A,G) canonical + revcomp dead (C,T), each with a het call."""
    svid = _seed_dbsnp_version(conn)
    _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
    _seed_variant(conn, 1, ref="A", alt="G", rsid=survivor_rsid)  # canonical survivor
    _seed_variant(conn, 2, ref="C", alt="T", rsid=dead_rsid)  # revcomp non-canonical
    _seed_call(conn, 10, 1, source="23andme", allele_1="A", allele_2="G")
    _seed_call(conn, 20, 2, source="ancestry", allele_1="C", allele_2="T")
    return svid


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def test_raises_when_no_dbsnp_loaded(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with duckdb_connection() as conn, pytest.raises(DbsnpNotLoadedError):
        collapse_duplicate_variants(conn)


# ---------------------------------------------------------------------------
# One test per mechanism
# ---------------------------------------------------------------------------


def test_collapse_strandflip(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Reverse-complement pair: the dead's chip call is complemented onto the survivor."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_strandflip_pair(conn)
        result = collapse_duplicate_variants(conn)

        assert isinstance(result, StrandCollapseResult)
        assert result.actionable_edges == 1
        assert result.strandflips_collapsed == 1
        assert result.calls_complemented == 1
        assert result.variants_master_deleted == 1
        assert result.calls_repointed == 1

        assert _variant_ids(conn) == [1]
        assert _dangling_calls(conn) == 0

        active = _active_calls_on(conn, 1)
        assert sorted(a[0] for a in active) == ["23andme", "ancestry"]
        for _src, a1, a2, _status in active:
            assert (a1, a2) == ("A", "G")
        ancestry = next(a for a in active if a[0] == "ancestry")
        assert ancestry[3] == "flipped_to_match"

        old = conn.execute(
            "SELECT is_active, superseded_by, superseded_reason, allele_1, allele_2 "
            "FROM genotype_calls WHERE call_id = 20",
        ).fetchone()
    assert old is not None
    assert old[0] is False
    assert old[1] is not None
    assert old[2] == "strand_flip_collapse_pr5"
    assert (old[3], old[4]) == ("C", "T")


def test_collapse_swap(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Same allele set, REF/ALT reversed, neither canonical: repoint VERBATIM, no complement.

    The anti-corruption case: complementing a same-strand swap would turn a real
    ``C/T`` into ``G/A``. The survivor is chip-over-imputed.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        # dbSNP entry at a different position -> both rows non-canonical.
        _seed_dbsnp_annotation(conn, 1, svid, pos=9999, ref="A", alts=("G",))
        _seed_variant(conn, 1, ref="C", alt="T")  # chip row
        _seed_variant(conn, 2, ref="T", alt="C")  # imputed row (swapped order)
        _seed_call(conn, 10, 1, source="23andme", allele_1="C", allele_2="T")
        _seed_call(
            conn, 20, 2, source="beagle_imputed", allele_1="T", allele_2="C", is_imputed=True
        )

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 1
        assert result.swaps_collapsed == 1
        assert result.calls_complemented == 0  # repoint, not complement
        assert result.variants_master_deleted == 1

        assert _variant_ids(conn) == [1]  # chip row survives (chip-over-imputed)
        assert _dangling_calls(conn) == 0
        # The imputed call rode the repoint verbatim — alleles unchanged.
        moved = conn.execute(
            "SELECT variant_id, allele_1, allele_2, is_active "
            "FROM genotype_calls WHERE call_id = 20",
        ).fetchone()
    assert moved is not None
    assert int(moved[0]) == 1
    assert (moved[1], moved[2]) == ("T", "C")
    assert moved[3] is True  # still active, not superseded


def test_collapse_hom_nocall(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A no-call (N,N) placeholder beside a real biallelic survivor: repoint the no-call."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        _seed_variant(conn, 1, ref="A", alt="G", rsid=None)  # canonical survivor
        _seed_variant(conn, 2, ref="N", alt="N", rsid="rs1")  # no-call placeholder
        _seed_call(conn, 10, 1, source="ancestry", allele_1="A", allele_2="G")
        _seed_nocall(conn, 20, 2, source="23andme")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 1
        assert result.no_call_repointed == 1
        assert result.calls_complemented == 0
        assert result.variants_master_deleted == 1
        assert _variant_ids(conn) == [1]
        assert _dangling_calls(conn) == 0
        # the no-call moved to the survivor, still a no-call
        assert _variant_id_of_call(conn, 20) == 1
        # the real rsID is recovered onto the (previously NULL) survivor
        assert _rsid_of(conn, 1) == "rs1"
        assert result.rsid_coalesced == 1


def test_collapse_hom_opp_strand(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Real-hom on the opposite strand (the chr4 shape): the hom call is complemented."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="C", alts=("T",))
        _seed_variant(conn, 1, ref="C", alt="T")  # canonical survivor
        _seed_variant(conn, 2, ref="G", alt="G")  # hom on the opposite strand
        _seed_call(conn, 10, 1, source="23andme", allele_1="C", allele_2="C")
        _seed_call(conn, 20, 2, source="ancestry", allele_1="G", allele_2="G")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 1
        assert result.hom_opp_collapsed == 1
        assert result.calls_complemented == 1
        assert _variant_ids(conn) == [1]
        active = _active_calls_on(conn, 1)
        ancestry = next(a for a in active if a[0] == "ancestry")
    assert (ancestry[1], ancestry[2]) == ("C", "C")  # G/G complemented to C/C
    assert ancestry[3] == "flipped_to_match"


def test_collapse_hom_same_strand(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Real-hom on the same strand: repoint verbatim, no complement."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="G", alts=("T",))
        _seed_variant(conn, 1, ref="G", alt="T")  # canonical survivor
        _seed_variant(conn, 2, ref="G", alt="G")  # hom, same strand (G in {G,T})
        _seed_call(conn, 10, 1, source="23andme", allele_1="G", allele_2="T")
        _seed_call(conn, 20, 2, source="ancestry", allele_1="G", allele_2="G")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 1
        assert result.hom_same_collapsed == 1
        assert result.calls_complemented == 0
        assert _variant_ids(conn) == [1]
        moved = conn.execute(
            "SELECT variant_id, allele_1, allele_2, is_active "
            "FROM genotype_calls WHERE call_id = 20",
        ).fetchone()
    assert moved is not None
    assert int(moved[0]) == 1
    assert (moved[1], moved[2]) == ("G", "G")  # unchanged
    assert moved[3] is True


def test_size3_bucket_mixed_drops_nocall_protects_multiallelic(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """N/N + two legit multi-allelic alts: the N/N is DROPPED, both alts survive.

    The rule-3 + DROP tripwire: ``(C,T)``/``(C,G)`` are a legit multi-allelic site
    (protected, never collapsed onto each other); the co-located ``(N,N)`` no-call
    has no single survivor, so it is dropped (not repointed onto an arbitrary alt).
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="C", alts=("T", "G"))
        _seed_variant(conn, 1, ref="N", alt="N", rsid="rs1")  # no-call placeholder -> DROP
        _seed_variant(conn, 2, ref="C", alt="T", rsid=None)  # canonical alt (protected)
        _seed_variant(conn, 3, ref="C", alt="G", rsid=None)  # canonical alt (protected)
        _seed_nocall(conn, 10, 1, source="23andme")
        _seed_call(
            conn, 20, 2, source="beagle_imputed", allele_1="C", allele_2="T", is_imputed=True
        )
        _seed_call(conn, 30, 3, source="ancestry", allele_1="C", allele_2="G")

        result = collapse_duplicate_variants(conn)
        assert result.no_call_dropped == 1
        assert result.no_call_repointed == 0
        assert result.legit_multiallelic_skipped == 1
        assert result.actionable_edges == 1  # the drop
        assert result.variants_master_deleted == 1
        # both multi-allelic alts survive untouched; the N/N is gone
        assert _variant_ids(conn) == [2, 3]
        assert _dangling_calls(conn) == 0
        # the no-call's call was deleted (not repointed onto an arbitrary alt)
        gone = conn.execute("SELECT COUNT(*) FROM genotype_calls WHERE call_id = 10").fetchone()
    assert gone is not None
    assert gone[0] == 0


# ---------------------------------------------------------------------------
# Protection + skips
# ---------------------------------------------------------------------------


def test_legit_multiallelic_untouched(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Two canonical alts (A,G)/(A,C) at one position are protected — nothing collapses."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G", "C"))
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_variant(conn, 2, ref="A", alt="C")
        _seed_call(conn, 10, 1, source="23andme", allele_1="A", allele_2="G")
        _seed_call(conn, 20, 2, source="ancestry", allele_1="A", allele_2="C")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 0
        assert result.legit_multiallelic_skipped == 1
        assert _variant_ids(conn) == [1, 2]


def test_non_revcomp_sibling_skipped(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A non-canonical different-alt sibling (A,C) is not a duplicate of (A,G) — skipped."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_variant(conn, 2, ref="A", alt="C")
        _seed_call(conn, 10, 1, source="23andme", allele_1="A", allele_2="G")
        _seed_call(conn, 20, 2, source="ancestry", allele_1="A", allele_2="C")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 0
        assert _variant_ids(conn) == [1, 2]


def test_genotype_mismatch_surfaced(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """N is the revcomp key of C, but its call's alleles resolve under neither strand."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_variant(conn, 2, ref="C", alt="T")
        _seed_call(conn, 10, 1, source="23andme", allele_1="A", allele_2="G")
        # A plus-strand A/G call mis-filed on the minus-strand (C,T) row:
        # complement(A/G) = T/C is not a subset of the survivor's {A,G}.
        _seed_call(conn, 20, 2, source="ancestry", allele_1="A", allele_2="G")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 0
        assert result.genotype_mismatch_skipped == 1
        assert _variant_ids(conn) == [1, 2]


def test_source_collision_skipped(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Reconciling would put two active calls of one source on the survivor — skip."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_variant(conn, 2, ref="C", alt="T")
        _seed_call(conn, 10, 1, source="23andme", allele_1="A", allele_2="G")
        _seed_call(conn, 20, 2, source="23andme", allele_1="C", allele_2="T")  # same source!

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 0
        assert result.source_collision_skipped == 1
        assert _variant_ids(conn) == [1, 2]


def test_imputed_call_on_dead_relocated_not_skipped(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The prior no-imputed-call guard is gone: an imputed call on N relocates to C."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_variant(conn, 2, ref="C", alt="T")
        _seed_call(conn, 10, 1, source="23andme", allele_1="A", allele_2="G")
        _seed_call(conn, 20, 2, source="ancestry", allele_1="C", allele_2="T")
        _seed_call(
            conn, 21, 2, source="beagle_imputed", allele_1="C", allele_2="T", is_imputed=True
        )

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 1
        assert result.strandflips_collapsed == 1
        assert result.calls_complemented == 2  # ancestry + beagle both complemented
        assert result.variants_master_deleted == 1
        assert _variant_ids(conn) == [1]
        assert _dangling_calls(conn) == 0
        # the imputed call complemented onto the survivor, still imputed
        imp = conn.execute(
            "SELECT allele_1, allele_2, is_imputed FROM genotype_calls "
            "WHERE variant_id = 1 AND source = 'beagle_imputed' AND is_active",
        ).fetchone()
    assert imp is not None
    assert (imp[0], imp[1]) == ("A", "G")
    assert imp[2] is True


def test_palindromic_survivor_skipped(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A palindromic survivor (A/T) is excluded — swap vs flip undecidable."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("T",))
        _seed_variant(conn, 1, ref="A", alt="T")  # palindromic, canonical
        _seed_variant(conn, 2, ref="A", alt="G")  # some sibling
        _seed_call(conn, 10, 1, source="23andme", allele_1="A", allele_2="T")
        _seed_call(conn, 20, 2, source="ancestry", allele_1="A", allele_2="G")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 0
        assert _variant_ids(conn) == [1, 2]


def test_no_call_collapses_at_palindromic_survivor(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A no-call at a palindromic survivor (T/A) still collapses.

    The per-edge guard exempts the no-call edge: it is strand-invariant (the empty
    call is repointed as-is, no complement decision), so the prior bucket-level skip
    that stranded it was wrong. Regression guard for that fix.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="T", alts=("A",))
        _seed_variant(conn, 1, ref="T", alt="A", rsid=None)  # palindromic, canonical survivor
        _seed_variant(conn, 2, ref="N", alt="N", rsid="rs1")  # no-call placeholder
        _seed_call(conn, 10, 1, source="ancestry", allele_1="T", allele_2="A")
        _seed_nocall(conn, 20, 2, source="23andme")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 1
        assert result.no_call_repointed == 1
        assert result.palindromic_skipped == 0
        assert result.calls_complemented == 0
        assert result.variants_master_deleted == 1
        assert _variant_ids(conn) == [1]
        assert _dangling_calls(conn) == 0
        # the no-call moved to the palindromic survivor, still a no-call
        assert _variant_id_of_call(conn, 20) == 1
        # the real rsID is recovered onto the (previously NULL) survivor
        assert _rsid_of(conn, 1) == "rs1"
        assert result.rsid_coalesced == 1


def test_swap_skipped_at_palindromic_survivor(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A strand-sensitive edge at a palindromic survivor is still skipped — now counted.

    A genuine strand-flip at a palindromic survivor is structurally a swap (the
    complement of {T,A} is {T,A}), so ``swap`` is the honest mechanism here, and swap
    vs strand-flip is undecidable at A/T — the edge is left un-collapsed and counted
    ``palindromic_skipped`` (the intended behavior the no-call exemption preserves).
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="T", alts=("A",))
        _seed_variant(conn, 1, ref="T", alt="A")  # palindromic, canonical survivor
        _seed_variant(conn, 2, ref="A", alt="T")  # swap: same alleles reversed, non-canonical
        _seed_call(conn, 10, 1, source="23andme", allele_1="T", allele_2="A")
        _seed_call(conn, 20, 2, source="ancestry", allele_1="A", allele_2="T")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 0
        assert result.palindromic_skipped == 1
        assert result.swaps_collapsed == 0
        assert _variant_ids(conn) == [1, 2]  # both rows survive untouched


def test_degenerate_no_survivor_skipped(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A (N,N) + a real-hom with no biallelic row: nothing to collapse onto — skip+warn."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, pos=9999, ref="A", alts=("G",))
        _seed_variant(conn, 1, ref="N", alt="N")
        _seed_variant(conn, 2, ref="G", alt="G")  # hom, non-canonical, not biallelic
        _seed_nocall(conn, 10, 1, source="23andme")
        _seed_call(conn, 20, 2, source="ancestry", allele_1="G", allele_2="G")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 0
        assert result.degenerate_skipped == 1
        assert _variant_ids(conn) == [1, 2]


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_mutates_nothing(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with duckdb_connection() as conn:
        _seed_strandflip_pair(conn, dead_rsid="rs1")
        result = collapse_duplicate_variants(conn, dry_run=True)

        assert result.dry_run is True
        assert result.actionable_edges == 1
        assert result.calls_complemented == 0
        assert result.variants_master_deleted == 0
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.survivor_id == 1
        assert edge.mechanism == "strandflip"
        assert edge.dead_variant_ids == (2,)
        assert edge.dead_rsids == ("rs1",)
        assert edge.calls_complemented == 1

        assert _variant_ids(conn) == [1, 2]
        still_active = conn.execute(
            "SELECT is_active FROM genotype_calls WHERE call_id = 20",
        ).fetchone()
    assert still_active is not None
    assert still_active[0] is True


# ---------------------------------------------------------------------------
# rsID coalesce
# ---------------------------------------------------------------------------


def test_rsid_coalesce_fills_null_survivor(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with duckdb_connection() as conn:
        _seed_strandflip_pair(conn, survivor_rsid=None, dead_rsid="rs1")
        result = collapse_duplicate_variants(conn)

        assert result.rsid_coalesced == 1
        assert result.rsid_conflicts == 0
        assert _rsid_of(conn, 1) == "rs1"


def test_rsid_conflict_keeps_survivor(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with duckdb_connection() as conn:
        _seed_strandflip_pair(conn, survivor_rsid="rsA", dead_rsid="rsB")
        result = collapse_duplicate_variants(conn)

        assert result.rsid_coalesced == 0
        assert result.rsid_conflicts == 1
        assert _rsid_of(conn, 1) == "rsA"  # survivor's own rsid wins


# ---------------------------------------------------------------------------
# Scaffold + idempotence + generality
# ---------------------------------------------------------------------------


def test_downstream_tables_cleared(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """``discrepancies`` (TX0) + consensus / index (TX1) are cleared by the run."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_strandflip_pair(conn)
        _seed_consensus(conn, 1)
        _seed_discrepancy(conn, 10, 1)
        conn.execute(
            "INSERT INTO variant_annotations_index (variant_id, last_refreshed) "
            "VALUES (1, CURRENT_TIMESTAMP)",
        )

        collapse_duplicate_variants(conn)

        counts = {
            t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # noqa: S608 — literal names
            for t in ("discrepancies", "consensus_genotypes", "variant_annotations_index")
        }
    assert counts == {"discrepancies": 0, "consensus_genotypes": 0, "variant_annotations_index": 0}


def test_idempotent_second_run(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with duckdb_connection() as conn:
        _seed_strandflip_pair(conn)
        first = collapse_duplicate_variants(conn)
        assert first.actionable_edges == 1

        second = collapse_duplicate_variants(conn)
    assert second.actionable_edges == 0
    assert second.variants_master_deleted == 0


def test_two_independent_pairs_collapse(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, chrom="1", pos=1000, ref="A", alts=("G",))
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G")
        _seed_variant(conn, 2, chrom="1", pos=1000, ref="C", alt="T")
        _seed_call(conn, 10, 1, source="23andme", allele_1="A", allele_2="G")
        _seed_call(conn, 20, 2, source="ancestry", allele_1="C", allele_2="T")
        _seed_dbsnp_annotation(conn, 2, svid, chrom="2", pos=2000, ref="A", alts=("C",))
        _seed_variant(conn, 3, chrom="2", pos=2000, ref="A", alt="C")
        _seed_variant(conn, 4, chrom="2", pos=2000, ref="G", alt="T")
        _seed_call(conn, 30, 3, source="23andme", allele_1="A", allele_2="C")
        _seed_call(conn, 40, 4, source="ancestry", allele_1="G", allele_2="T")

        result = collapse_duplicate_variants(conn)
        assert result.actionable_edges == 2
        assert result.strandflips_collapsed == 2
        assert result.variants_master_deleted == 2
        assert result.calls_complemented == 2
        assert _variant_ids(conn) == [1, 3]
        assert _dangling_calls(conn) == 0


def test_inactive_call_on_dead_is_repointed(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """An inactive (prior-superseded) call on N is re-pointed so the orphan deletes cleanly."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_strandflip_pair(conn)
        _seed_call(conn, 21, 2, source="ancestry", allele_1="C", allele_2="T", is_active=False)

        result = collapse_duplicate_variants(conn)
        assert result.variants_master_deleted == 1
        assert _dangling_calls(conn) == 0
        assert _variant_id_of_call(conn, 21) == 1


def test_force_on_empty_is_noop_without_error(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_call(conn, 10, 1, source="23andme", allele_1="A", allele_2="G")

        result = collapse_duplicate_variants(conn, force=True)
        assert result.actionable_edges == 0
        assert result.variants_master_deleted == 0
        assert _variant_ids(conn) == [1]


# ---------------------------------------------------------------------------
# Integration: the survivor re-merges correctly after collapse
# ---------------------------------------------------------------------------


def test_integration_strandflip_merges_to_both_concordant(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Post-collapse the survivor carries two same-strand chip calls that agree, so
    ``genome merge`` resolves it as a single ``both_concordant`` consensus."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_strandflip_pair(conn)
        collapse_duplicate_variants(conn)

    merge_all()

    with duckdb_connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT consensus_method, consensus_allele_1, consensus_allele_2 "
            "FROM consensus_genotypes WHERE variant_id = 1",
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM consensus_genotypes").fetchone()
        flips = conn.execute(
            "SELECT COUNT(*) FROM consensus_genotypes "
            "WHERE consensus_method = 'disagreement_resolved'",
        ).fetchone()
    assert row is not None
    assert (row[0], row[1], row[2]) == ("both_concordant", "A", "G")
    assert total is not None
    assert total[0] == 1
    assert flips is not None
    assert flips[0] == 0


def test_integration_nocall_survivor_stays_imputed_only(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """An imputed survivor + a repointed chip no-call re-merges to ``imputed_only``.

    This is the PR-5b-pre (finding-028) dependency: without the consensus_v1
    chip-no-call fix, ``merge`` would clobber the imputed genotype to a no-call.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        _seed_variant(conn, 1, ref="A", alt="G", rsid=None)  # imputed-only survivor
        _seed_variant(conn, 2, ref="N", alt="N", rsid="rs1")  # chip no-call placeholder
        _seed_call(
            conn, 10, 1, source="beagle_imputed", allele_1="A", allele_2="G", is_imputed=True
        )
        _seed_nocall(conn, 20, 2, source="23andme")
        collapse_duplicate_variants(conn)

    merge_all()

    with duckdb_connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT consensus_method, consensus_allele_1, consensus_allele_2, is_imputed "
            "FROM consensus_genotypes WHERE variant_id = 1",
        ).fetchone()
    assert row is not None
    assert (row[0], row[1], row[2]) == ("imputed_only", "A", "G")
    assert row[3] is True
