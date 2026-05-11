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
    or ``None`` when that source has no active call on this row.
    """

    variant_id: int
    rsid: str
    chrom: str
    pos: int
    ref: str
    alt: str
    call_23: tuple[str, str, bool] | None
    call_anc: tuple[str, str, bool] | None


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
) -> None:
    conn.execute(
        """
        INSERT INTO genotype_calls (
            call_id, variant_id, source, source_chip_version, ingestion_run_id,
            genotype_raw, allele_1, allele_2, is_no_call,
            is_imputed, raw_strand, strand_status, quality_flags, is_active
        ) VALUES (?, ?, ?::source_enum, 'test', ?,
                  ?, ?, ?, ?, FALSE, '+',
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
        (7, "genotype_mismatch", "info", "flipped_strand_match"),
        (8, "genotype_mismatch", "info", "flipped_strand_match"),
    ]


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
    # 1 both_concordant; 1 unresolvable genotype_mismatch; 1 unresolvable strand_ambiguous;
    # 1 single_source no_call_diff; 2 single_source platform_unique;
    # 2 disagreement_resolved from tier-3 flip.
    assert result.method_counts == {
        "both_concordant": 1,
        "unresolvable": 2,
        "single_source": 3,
        "disagreement_resolved": 2,
    }
    assert result.discrepancy_type_counts == {
        "genotype_mismatch": 3,  # 1 unresolved + 2 strand-flip
        "strand_ambiguous": 1,
        "no_call_diff": 1,
        "platform_unique": 2,
    }
    assert result.severity_counts == {
        "major": 1,
        "minor": 2,
        "info": 4,
    }
    assert result.strand_flip_resolutions == 2
    # Concordance denominator: concordant + flip-resolved shared variants vs
    # those plus genuine discords (genotype_mismatch and strand_ambiguous,
    # minus the flip-resolved cases which are not biological discords).
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
    assert "consensus_rows=8" in result.output
    assert "strand_flips=2" in result.output
    assert "discrepancy_types:" in result.output


@pytest.mark.parametrize(
    "variant_id",
    [1, 2, 3, 4, 5, 6, 7, 8],
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
