"""End-to-end merge pipeline tests against a real DuckDB.

The deliverables call for synthetic fixtures covering each of: both
platforms agree, ``genotype_mismatch``, ``strand_ambiguous`` palindrome,
strand-flip resolution (tier 3), ``platform_unique``, and ``no_call_diff``.
Rather than reusing the on-disk Phase 2 fixture files, this module seeds
``variants_master`` and ``genotype_calls`` directly with hand-built rows so
each discrepancy type lands at a known ``variant_id`` and the assertions
read cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from genome.cli import app
from genome.db import duckdb_connection, init_databases
from genome.merge import MERGE_VERSION, merge_all

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


@dataclass(frozen=True)
class Site:
    """Convenience tuple for seeding a variants_master row + its calls.

    Keep the fields in 1:1 correspondence with the parts of the schema we
    need at merge time. ``call_23`` / ``call_anc`` are ``(a1, a2, is_no_call)``
    or ``None`` when that source has no active call on this row. ``call_imp``
    is ``(a1, a2, is_no_call, imputation_r2)`` for the Phase 4 imputed
    source or ``None`` when no imputed call exists at this variant.
    """

    variant_id: int
    rsid: str
    chrom: str
    pos: int
    ref: str
    alt: str
    call_23: tuple[str, str, bool] | None
    call_anc: tuple[str, str, bool] | None
    call_imp: tuple[str, str, bool, float] | None = None


def _insert_variants(conn: DuckDBPyConnection, sites: list[Site]) -> None:
    for s in sites:
        conn.execute(
            """
            INSERT INTO variants_master (
                variant_id, rsid, chrom, pos_grch38, pos_grch37,
                ref_allele, alt_allele, variant_type,
                has_genotyped_call, has_imputed_call, is_acmg_sf,
                liftover_chain, liftover_status
            ) VALUES (?, ?, ?::chromosome_enum, ?, ?, ?, ?, 'SNV',
                      TRUE, FALSE, FALSE, 'native_grch38', 'native_grch38')
            """,
            [s.variant_id, s.rsid, s.chrom, s.pos, s.pos, s.ref, s.alt],
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
    is_no_call: bool,
    is_imputed: bool = False,
    imputation_r2: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO genotype_calls (
            call_id, variant_id, source, source_chip_version, ingestion_run_id,
            genotype_raw, allele_1, allele_2, is_no_call,
            is_imputed, imputation_r2, imputation_panel,
            raw_strand, strand_status, quality_flags, is_active
        ) VALUES (?, ?, ?::source_enum, 'test', ?,
                  ?, ?, ?, ?, ?, ?, ?,
                  '+',
                  CASE WHEN ? THEN 'unknown'::strand_status_enum
                       ELSE 'resolved_plus'::strand_status_enum END,
                  ARRAY[]::VARCHAR[], TRUE)
        """,
        [
            call_id,
            variant_id,
            source,
            run_id,
            "--" if is_no_call else (allele_1 + allele_2),
            None if is_no_call else allele_1,
            None if is_no_call else allele_2,
            is_no_call,
            is_imputed,
            imputation_r2,
            "1000g_phase3_grch38" if is_imputed else None,
            is_no_call,
        ],
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
        [run_id, source, f"/test/run_{run_id}", f"{'0' * 64}"],
    )


def _seed(conn: DuckDBPyConnection, sites: list[Site]) -> None:
    """Seed variants_master, genotype_calls, ingestion_runs for the merge to chew on."""
    _insert_ingestion_run(conn, 1, "23andme")
    _insert_ingestion_run(conn, 2, "ancestry")
    # Only register the imputed ingestion_run lazily — most fixtures don't need it.
    imputed_run_registered = False
    _insert_variants(conn, sites)

    next_call_id = 1
    for s in sites:
        if s.call_23 is not None:
            a1, a2, nc = s.call_23
            _insert_call(
                conn,
                call_id=next_call_id,
                variant_id=s.variant_id,
                source="23andme",
                run_id=1,
                allele_1=a1,
                allele_2=a2,
                is_no_call=nc,
            )
            next_call_id += 1
        if s.call_anc is not None:
            a1, a2, nc = s.call_anc
            _insert_call(
                conn,
                call_id=next_call_id,
                variant_id=s.variant_id,
                source="ancestry",
                run_id=2,
                allele_1=a1,
                allele_2=a2,
                is_no_call=nc,
            )
            next_call_id += 1
        if s.call_imp is not None:
            if not imputed_run_registered:
                _insert_ingestion_run(conn, 3, "beagle_imputed")
                imputed_run_registered = True
            a1, a2, nc, r2 = s.call_imp
            _insert_call(
                conn,
                call_id=next_call_id,
                variant_id=s.variant_id,
                source="beagle_imputed",
                run_id=3,
                allele_1=a1,
                allele_2=a2,
                is_no_call=nc,
                is_imputed=True,
                imputation_r2=r2,
            )
            next_call_id += 1


# ----------------------------------------------------------------------------
# The synthetic test corpus: one row per discrepancy type, plus tier-3 strand
# flip cases. Variant IDs are hand-assigned so each assertion is unambiguous
# about which row it is checking.
# ----------------------------------------------------------------------------

CORPUS: list[Site] = [
    # 1) Both platforms agree (no discrepancy).
    Site(
        variant_id=1,
        rsid="rs_both_concordant",
        chrom="1",
        pos=1000,
        ref="A",
        alt="G",
        call_23=("A", "G", False),
        call_anc=("A", "G", False),
    ),
    # 2) Both platforms call, genotype_mismatch at a non-palindromic site whose
    #    complement does NOT match — true biological disagreement.
    Site(
        variant_id=2,
        rsid="rs_genotype_mismatch",
        chrom="1",
        pos=2000,
        ref="A",
        alt="G",
        call_23=("A", "G", False),
        # C/G is not the complement of A/G (complement(A/G) sorted is C/T).
        call_anc=("C", "G", False),
    ),
    # 3) Strand-ambiguous palindromic A/T site with disagreement.
    Site(
        variant_id=3,
        rsid="rs_strand_ambiguous",
        chrom="1",
        pos=3000,
        ref="A",
        alt="T",
        call_23=("A", "A", False),
        call_anc=("T", "T", False),
    ),
    # 4) no_call_diff: 23andme calls, ancestry no-call.
    Site(
        variant_id=4,
        rsid="rs_no_call_diff",
        chrom="1",
        pos=4000,
        ref="A",
        alt="G",
        call_23=("A", "G", False),
        call_anc=("", "", True),
    ),
    # 5) platform_unique: only 23andme has an active call on this row.
    Site(
        variant_id=5,
        rsid="rs_platform_unique_23",
        chrom="1",
        pos=5000,
        ref="C",
        alt="T",
        call_23=("C", "T", False),
        call_anc=None,
    ),
    # 6) platform_unique: only ancestry has an active call on this row.
    Site(
        variant_id=6,
        rsid="rs_platform_unique_anc",
        chrom="1",
        pos=6000,
        ref="A",
        alt="C",
        call_23=None,
        call_anc=("A", "C", False),
    ),
    # 7+8) Tier-3 strand-flip: same (chrom, pos), complementary alleles, each row
    #      has one source's call. The merge should rewrite both rows to
    #      disagreement_resolved + genotype_mismatch with flipped_strand_match.
    Site(
        variant_id=7,
        rsid="rs_strand_flip_a",
        chrom="1",
        pos=7000,
        ref="A",
        alt="G",
        call_23=("A", "G", False),
        call_anc=None,
    ),
    Site(
        variant_id=8,
        rsid="rs_strand_flip_b",
        chrom="1",
        pos=7000,  # same position as row 7
        ref="C",
        alt="T",
        call_23=None,
        call_anc=("C", "T", False),  # complement of A/G after sort
    ),
    # 9) Phase 4: imputed-only, called. Only beagle_imputed has an active call.
    Site(
        variant_id=9,
        rsid="rs_imputed_only_called",
        chrom="1",
        pos=9000,
        ref="A",
        alt="G",
        call_23=None,
        call_anc=None,
        call_imp=("A", "G", False, 0.92),
    ),
    # 10) Phase 4: imputed-only, no-call.
    Site(
        variant_id=10,
        rsid="rs_imputed_only_nocall",
        chrom="1",
        pos=10000,
        ref="C",
        alt="T",
        call_23=None,
        call_anc=None,
        call_imp=("", "", True, 0.15),
    ),
    # 11) Phase 4: 23andme + beagle_imputed at the same variant.
    #     Chip resolution prevails; imputed contributes confirming evidence.
    Site(
        variant_id=11,
        rsid="rs_chip23_plus_imputed",
        chrom="1",
        pos=11000,
        ref="A",
        alt="G",
        call_23=("A", "G", False),
        call_anc=None,
        call_imp=("A", "G", False, 0.88),
    ),
    # 12) Phase 4: both chips concordant + beagle_imputed. Chip both_concordant
    #     stays; imputed appended to contributing_calls.
    Site(
        variant_id=12,
        rsid="rs_both_chips_plus_imputed",
        chrom="1",
        pos=12000,
        ref="A",
        alt="G",
        call_23=("A", "G", False),
        call_anc=("A", "G", False),
        call_imp=("A", "G", False, 0.95),
    ),
    # 13+14) Phase 4: tier-3 strand-flip candidate where one row also has an
    #     active beagle_imputed call. Our implementation excludes pairs with
    #     imputed calls from tier-3 candidacy (so the imputed call is not
    #     dropped from contributing_calls); both rows stay single_source.
    Site(
        variant_id=13,
        rsid="rs_tier3_flip_with_imputed_a",
        chrom="1",
        pos=13000,
        ref="A",
        alt="G",
        call_23=("A", "G", False),
        call_anc=None,
        call_imp=None,
    ),
    Site(
        variant_id=14,
        rsid="rs_tier3_flip_with_imputed_b",
        chrom="1",
        pos=13000,  # same (chrom, pos) as row 13
        ref="C",
        alt="T",
        call_23=None,
        call_anc=("C", "T", False),  # complement of A/G after sort
        call_imp=("C", "T", False, 0.80),  # confirming evidence on this row
    ),
]


def _run_merge_against_corpus(tmp_path: Path) -> tuple[str, ...]:
    """Init DB, seed corpus, run merge_all, return DB path string."""
    init_databases()
    db = tmp_path / "genome.duckdb"
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all(duckdb_path=db)
    return (str(db),)


def _duckdb_path(env: dict[str, str]) -> Path:
    return Path(env["GENOME_DUCKDB_PATH"])


def test_merge_writes_one_consensus_per_variant(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        master_n = conn.execute("SELECT COUNT(*) FROM variants_master").fetchone()[0]
        consensus_n = conn.execute("SELECT COUNT(*) FROM consensus_genotypes").fetchone()[0]
    assert master_n == len(CORPUS)
    assert consensus_n == master_n


def test_merge_both_concordant_no_discrepancy(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        row = conn.execute(
            "SELECT consensus_method, consensus_allele_1, consensus_allele_2,"
            "       is_no_call, dosage, resolution_rule"
            " FROM consensus_genotypes WHERE variant_id = 1",
        ).fetchone()
        discs = conn.execute(
            "SELECT COUNT(*) FROM discrepancies WHERE variant_id = 1",
        ).fetchone()
    assert row == ("both_concordant", "A", "G", False, 1, MERGE_VERSION)
    assert discs == (0,)


def test_merge_genotype_mismatch_major_severity(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        consensus = conn.execute(
            "SELECT consensus_method, is_no_call FROM consensus_genotypes WHERE variant_id = 2",
        ).fetchone()
        disc = conn.execute(
            "SELECT discrepancy_type, severity, resolution, source_a, source_b,"
            "       genotype_a, genotype_b"
            " FROM discrepancies WHERE variant_id = 2",
        ).fetchone()
    assert consensus == ("unresolvable", True)
    assert disc == (
        "genotype_mismatch",
        "major",
        "unresolved",
        "23andme",
        "ancestry",
        "A/G",
        "C/G",
    )


def test_merge_strand_ambiguous_palindrome_minor_severity(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        consensus = conn.execute(
            "SELECT consensus_method, is_no_call FROM consensus_genotypes WHERE variant_id = 3",
        ).fetchone()
        disc = conn.execute(
            "SELECT discrepancy_type, severity FROM discrepancies WHERE variant_id = 3",
        ).fetchone()
    assert consensus == ("unresolvable", True)
    assert disc == ("strand_ambiguous", "minor")


def test_merge_no_call_diff(isolated_settings: dict[str, str]) -> None:
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        consensus = conn.execute(
            "SELECT consensus_method, consensus_allele_1, consensus_allele_2"
            " FROM consensus_genotypes WHERE variant_id = 4",
        ).fetchone()
        disc = conn.execute(
            "SELECT discrepancy_type, severity, source_a, source_b, genotype_b"
            " FROM discrepancies WHERE variant_id = 4",
        ).fetchone()
    assert consensus == ("single_source", "A", "G")
    assert disc == ("no_call_diff", "minor", "23andme", "ancestry", "--")


def test_merge_platform_unique_each_source(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        row_23 = conn.execute(
            "SELECT consensus_method, discrepancy_type, severity, source_a, source_b"
            " FROM consensus_genotypes cg"
            " JOIN discrepancies d ON d.variant_id = cg.variant_id"
            " WHERE cg.variant_id = 5",
        ).fetchone()
        row_anc = conn.execute(
            "SELECT consensus_method, discrepancy_type, severity, source_a, source_b"
            " FROM consensus_genotypes cg"
            " JOIN discrepancies d ON d.variant_id = cg.variant_id"
            " WHERE cg.variant_id = 6",
        ).fetchone()
    assert row_23 == ("single_source", "platform_unique", "info", "23andme", None)
    assert row_anc == ("single_source", "platform_unique", "info", "ancestry", None)


def test_merge_tier3_strand_flip_resolves_across_rows(
    isolated_settings: dict[str, str],
) -> None:
    """Tier-3 cross-row flip writes ``strand_flip_resolved`` + ``disagreement_resolved``.

    The resolution is *successful* — the audit row is the new
    ``strand_flip_resolved`` type at ``info`` severity, not the misleading
    ``genotype_mismatch`` that the earlier classification used.
    """
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        # Both rows should now be disagreement_resolved with both call_ids
        # contributing.
        rows = conn.execute(
            "SELECT variant_id, consensus_method, consensus_allele_1, consensus_allele_2,"
            "       array_length(contributing_calls)"
            " FROM consensus_genotypes WHERE variant_id IN (7, 8)"
            " ORDER BY variant_id",
        ).fetchall()
        discs = conn.execute(
            "SELECT variant_id, discrepancy_type, severity, resolution"
            " FROM discrepancies WHERE variant_id IN (7, 8)"
            " ORDER BY variant_id",
        ).fetchall()
    assert rows == [
        (7, "disagreement_resolved", "A", "G", 2),
        (8, "disagreement_resolved", "C", "T", 2),
    ]
    assert discs == [
        (7, "strand_flip_resolved", "info", "flipped_strand_match"),
        (8, "strand_flip_resolved", "info", "flipped_strand_match"),
    ]


def test_merge_distinguishes_resolved_flip_from_genuine_mismatch(
    isolated_settings: dict[str, str],
) -> None:
    """A successful strand flip and a genuine mismatch land in different buckets.

    Variant 7+8 in the corpus are the tier-3 cross-row strand-flip pair: their
    consensus method is ``disagreement_resolved`` and their discrepancy type is
    ``strand_flip_resolved`` at ``info`` severity. Variant 2 is a non-palindromic
    site whose complement flip does **not** reconcile the alleles: its consensus
    method is ``unresolvable`` and the discrepancy is ``genotype_mismatch`` at
    ``major`` severity. The two cases must never be conflated.
    """
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        resolved = conn.execute(
            "SELECT cg.consensus_method, d.discrepancy_type, d.severity, d.resolution"
            "  FROM consensus_genotypes cg"
            "  JOIN discrepancies d ON d.variant_id = cg.variant_id"
            " WHERE cg.variant_id IN (7, 8)"
            " ORDER BY cg.variant_id",
        ).fetchall()
        unresolved = conn.execute(
            "SELECT cg.consensus_method, cg.is_no_call,"
            "       d.discrepancy_type, d.severity, d.resolution"
            "  FROM consensus_genotypes cg"
            "  JOIN discrepancies d ON d.variant_id = cg.variant_id"
            " WHERE cg.variant_id = 2",
        ).fetchone()

    # Successful tier-3 strand-flip resolutions: clean consensus, audit-only
    # strand_flip_resolved discrepancy at info severity.
    assert resolved == [
        ("disagreement_resolved", "strand_flip_resolved", "info", "flipped_strand_match"),
        ("disagreement_resolved", "strand_flip_resolved", "info", "flipped_strand_match"),
    ]
    # Genuine disagreement that the complement flip cannot reconcile: consensus
    # held as no-call and the discrepancy is a real genotype_mismatch at major.
    assert unresolved == (
        "unresolvable",
        True,
        "genotype_mismatch",
        "major",
        "unresolved",
    )


def test_merge_is_idempotent(isolated_settings: dict[str, str]) -> None:
    """Running ``merge_all`` twice yields the same row counts; no duplication."""
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)

    first = merge_all()
    second = merge_all()
    assert first.consensus_rows_written == second.consensus_rows_written
    assert first.discrepancy_rows_written == second.discrepancy_rows_written

    with duckdb_connection(db, read_only=True) as conn:
        consensus_n = conn.execute("SELECT COUNT(*) FROM consensus_genotypes").fetchone()[0]
        disc_n = conn.execute("SELECT COUNT(*) FROM discrepancies").fetchone()[0]
    assert consensus_n == first.consensus_rows_written
    assert disc_n == first.discrepancy_rows_written


def test_merge_result_summary_counts(isolated_settings: dict[str, str]) -> None:
    """Method / type / severity rollups land on the result object correctly."""
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)

    result = merge_all()
    # 1 both_concordant + 1 chip+chip+imputed both_concordant (v12);
    # 2 unresolvable (genotype_mismatch + strand_ambiguous);
    # 6 single_source (v4 no_call_diff, v5/v6 platform_unique, v11 chip+imputed,
    #   v13/v14 tier-3 candidate excluded because v14 carries an imputed call);
    # 2 disagreement_resolved from the v7+v8 tier-3 flip;
    # 2 imputed_only (v9 called, v10 no-call).
    assert result.method_counts == {
        "both_concordant": 2,
        "unresolvable": 2,
        "single_source": 6,
        "disagreement_resolved": 2,
        "imputed_only": 2,
    }
    # genotype_mismatch is strictly the unresolved biological disagreement now;
    # the 2 tier-3 strand-flip rows land in their own strand_flip_resolved bucket.
    # platform_unique gains v11 (chip+imputed at platform-unique site) and
    # v13/v14 (tier-3 candidate held back by the imputed-bearing guard).
    assert result.discrepancy_type_counts == {
        "genotype_mismatch": 1,
        "strand_flip_resolved": 2,
        "strand_ambiguous": 1,
        "no_call_diff": 1,
        "platform_unique": 5,
    }
    assert result.severity_counts == {
        "major": 1,
        "minor": 2,
        "info": 7,
    }
    assert result.strand_flip_resolutions == 2
    # Concordance denominator: concordant + flip-resolved shared variants vs
    # those plus genuine discords (genotype_mismatch and strand_ambiguous).
    # strand_flip_resolved rows are successful reconciliations and do not
    # count against concordance.
    assert result.concordance_rate is not None
    assert 0.5 < result.concordance_rate <= 1.0


def test_merge_cli_runs_end_to_end(isolated_settings: dict[str, str]) -> None:
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)

    runner = CliRunner()
    result = runner.invoke(app, ["merge"])
    assert result.exit_code == 0, result.output
    assert f"rule={MERGE_VERSION}" in result.output
    assert f"consensus_rows={len(CORPUS)}" in result.output
    assert "strand_flips=2" in result.output
    assert "discrepancy_types:" in result.output


@pytest.mark.parametrize(
    "variant_id",
    [s.variant_id for s in CORPUS],
)
def test_call_comparison_view_returns_each_variant(
    isolated_settings: dict[str, str],
    variant_id: int,
) -> None:
    """The pre-existing ``call_comparison_v`` view should now see consensus values."""
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        row = conn.execute(
            "SELECT consensus_method FROM call_comparison_v WHERE variant_id = ?",
            [variant_id],
        ).fetchone()
    assert row is not None
    assert row[0] is not None


# ----------------------------------------------------------------------------
# Phase 4 — imputation as third source. The fixtures below assert each branch
# of the consensus_v1 extension: imputed-only (called and no-call), chip plus
# imputed (single_source and both_concordant cases), and the tier-3 strand-
# flip guard that excludes imputed-bearing rows from tier-3 candidacy.
# ----------------------------------------------------------------------------


def test_merge_imputed_only_called(isolated_settings: dict[str, str]) -> None:
    """Variant with only a beagle_imputed call resolves as ``imputed_only``.

    The consensus carries the imputed call's alleles and the per-variant
    imputation_r2 propagates to ``consensus_genotypes.consensus_r2``. No
    discrepancy is emitted — an imputed-only call is a thin source, not a
    disagreement with anything.
    """
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        consensus = conn.execute(
            "SELECT consensus_method, consensus_allele_1, consensus_allele_2,"
            "       is_no_call, dosage, is_imputed, consensus_r2,"
            "       array_length(contributing_calls)"
            "  FROM consensus_genotypes WHERE variant_id = 9",
        ).fetchone()
        disc_n = conn.execute(
            "SELECT COUNT(*) FROM discrepancies WHERE variant_id = 9",
        ).fetchone()
    assert consensus is not None
    method, a1, a2, is_no_call, dosage, is_imputed, r2, contributing_n = consensus
    assert method == "imputed_only"
    assert (a1, a2) == ("A", "G")
    assert is_no_call is False
    assert dosage == 1  # A/G het with alt='G'
    assert is_imputed is True
    assert r2 is not None
    assert abs(r2 - 0.92) < 1e-6
    assert contributing_n == 1
    assert disc_n == (0,)


def test_merge_imputed_only_no_call(isolated_settings: dict[str, str]) -> None:
    """Variant with only a beagle_imputed no-call resolves as ``imputed_only`` no-call.

    is_imputed remains True and the imputation_r2 propagates even though the
    call itself is a no-call — downstream filters can still see that this
    site was looked up against the panel.
    """
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        consensus = conn.execute(
            "SELECT consensus_method, is_no_call, is_imputed, consensus_r2"
            "  FROM consensus_genotypes WHERE variant_id = 10",
        ).fetchone()
        disc_n = conn.execute(
            "SELECT COUNT(*) FROM discrepancies WHERE variant_id = 10",
        ).fetchone()
    assert consensus is not None
    method, is_no_call, is_imputed, r2 = consensus
    assert method == "imputed_only"
    assert is_no_call is True
    assert is_imputed is True
    assert r2 is not None
    assert abs(r2 - 0.15) < 1e-6
    assert disc_n == (0,)


def test_merge_chip_plus_imputed_chip_prevails(
    isolated_settings: dict[str, str],
) -> None:
    """Chip + imputed at the same variant: chip resolution stays, imputed appended.

    Variant 11 has a 23andme call A/G and a beagle_imputed call A/G. The
    consensus method must remain ``single_source`` (the chip-only result),
    ``is_imputed`` stays False, alleles/dosage come from the chip call, and
    ``contributing_calls`` carries both call_ids — chip first, imputed
    appended.
    """
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        consensus = conn.execute(
            "SELECT consensus_method, consensus_allele_1, consensus_allele_2,"
            "       is_imputed, consensus_r2,"
            "       array_length(contributing_calls)"
            "  FROM consensus_genotypes WHERE variant_id = 11",
        ).fetchone()
        # Verify the two call_ids in contributing_calls map to the chip 23andme
        # call and the beagle_imputed call respectively, in that order.
        sources = conn.execute(
            "SELECT gc.source"
            "  FROM consensus_genotypes cg, UNNEST(cg.contributing_calls)"
            "       WITH ORDINALITY AS u(call_id, ord)"
            "  JOIN genotype_calls gc ON gc.call_id = u.call_id"
            " WHERE cg.variant_id = 11"
            " ORDER BY u.ord",
        ).fetchall()
    assert consensus is not None
    method, a1, a2, is_imputed, r2, contributing_n = consensus
    assert method == "single_source"
    assert (a1, a2) == ("A", "G")
    assert is_imputed is False
    # consensus_r2 is left None on chip-derived rows: imputation is confirming
    # evidence, not the consensus source. The per-call imputation_r2 is still
    # discoverable via the imputed call_id in contributing_calls.
    assert r2 is None
    assert contributing_n == 2
    assert [s for (s,) in sources] == ["23andme", "beagle_imputed"]


def test_merge_both_chips_plus_imputed_concordant(
    isolated_settings: dict[str, str],
) -> None:
    """Both chips concordant + imputed: consensus stays both_concordant, imputed appended.

    Variant 12 has 23andme A/G, ancestry A/G, and beagle_imputed A/G all
    agreeing. The chip both_concordant resolution must stand byte-for-byte
    (alleles, dosage, is_imputed=False, confidence=0.99); contributing_calls
    grows from two to three entries, with the imputed call_id appended last.
    """
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        consensus = conn.execute(
            "SELECT consensus_method, consensus_allele_1, consensus_allele_2,"
            "       is_imputed, dosage, confidence,"
            "       array_length(contributing_calls)"
            "  FROM consensus_genotypes WHERE variant_id = 12",
        ).fetchone()
        sources = conn.execute(
            "SELECT gc.source"
            "  FROM consensus_genotypes cg, UNNEST(cg.contributing_calls)"
            "       WITH ORDINALITY AS u(call_id, ord)"
            "  JOIN genotype_calls gc ON gc.call_id = u.call_id"
            " WHERE cg.variant_id = 12"
            " ORDER BY u.ord",
        ).fetchall()
        disc_n = conn.execute(
            "SELECT COUNT(*) FROM discrepancies WHERE variant_id = 12",
        ).fetchone()
    assert consensus is not None
    method, a1, a2, is_imputed, dosage, confidence, contributing_n = consensus
    assert method == "both_concordant"
    assert (a1, a2) == ("A", "G")
    assert is_imputed is False
    assert dosage == 1
    assert confidence is not None
    assert abs(float(confidence) - 0.99) < 1e-6
    assert contributing_n == 3
    assert [s for (s,) in sources] == ["23andme", "ancestry", "beagle_imputed"]
    # both_concordant emits no discrepancy.
    assert disc_n == (0,)


def test_merge_tier3_excludes_pairs_with_imputed_call(
    isolated_settings: dict[str, str],
) -> None:
    """Tier-3 candidacy is suppressed when any candidate row carries an imputed call.

    Variants 13 and 14 sit at the same (chrom, pos) with complementary
    alleles — exactly the shape that would trigger a tier-3 strand-flip
    rewrite. But variant 14 also has an active beagle_imputed call. Letting
    the tier-3 rewrite proceed would replace v14's contributing_calls with
    (23andme_id, ancestry_id) and drop the imputed call from the audit trail.
    The implementation therefore excludes any pair with an imputed call from
    tier-3 candidacy: both rows stay ``single_source`` with the imputed call
    preserved on v14.
    """
    init_databases()
    db = _duckdb_path(isolated_settings)
    with duckdb_connection(db) as conn:
        _seed(conn, CORPUS)
    merge_all()

    with duckdb_connection(db, read_only=True) as conn:
        rows = conn.execute(
            "SELECT variant_id, consensus_method, array_length(contributing_calls)"
            "  FROM consensus_genotypes WHERE variant_id IN (13, 14)"
            "  ORDER BY variant_id",
        ).fetchall()
        # No strand_flip_resolved discrepancies for these two rows; the only
        # discrepancies should be platform_unique at info severity.
        discs = conn.execute(
            "SELECT variant_id, discrepancy_type, severity"
            "  FROM discrepancies WHERE variant_id IN (13, 14)"
            "  ORDER BY variant_id",
        ).fetchall()
        v14_sources = conn.execute(
            "SELECT gc.source"
            "  FROM consensus_genotypes cg, UNNEST(cg.contributing_calls)"
            "       WITH ORDINALITY AS u(call_id, ord)"
            "  JOIN genotype_calls gc ON gc.call_id = u.call_id"
            " WHERE cg.variant_id = 14"
            " ORDER BY u.ord",
        ).fetchall()
    assert rows == [
        (13, "single_source", 1),  # 23andme only
        (14, "single_source", 2),  # ancestry + imputed
    ]
    assert discs == [
        (13, "platform_unique", "info"),
        (14, "platform_unique", "info"),
    ]
    # Imputed call is preserved on v14's contributing_calls — that is exactly
    # what the tier-3 exclusion protects against.
    assert [s for (s,) in v14_sources] == ["ancestry", "beagle_imputed"]
