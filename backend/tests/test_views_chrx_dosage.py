"""Tests for ``consensus_chrx_dosage_v`` (PR 5a) — built through ``genome init``.

These run the view through the real ``init_databases`` / DDL path (which raises
on a view-compile failure), so they double as proof the view compiles under
DuckDB. They also pin the two parities the view must hold: its non-PAR predicate
equals :func:`genome.par_regions.is_nonpar`, and its in-SQL ``profile_sex`` rule
equals :func:`genome.imputation.sex.profile_sex_label`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from genome.db import duckdb_connection, init_databases
from genome.db.init_schema import materialize_view
from genome.imputation.sex import profile_sex_label
from genome.par_regions import is_nonpar

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

# A genuine non-PAR-core position (> PAR1_END 2,781,479, < PAR2_START 155,701,383).
_NONPAR_CORE = 50_000_000


def _insert_chip_qc(conn: DuckDBPyConnection, run_id: int, source: str, sex: str) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_runs (
            run_id, source, file_path, file_hash_sha256, status, pipeline_version
        ) VALUES (?, ?::source_enum, ?, ?, 'completed', 'test')
        """,
        [run_id, source, f"/t/run_{run_id}", "0" * 64],
    )
    conn.execute(
        "INSERT INTO sample_qc (qc_id, run_id, sex_inferred, qc_status) VALUES (?, ?, ?, 'pass')",
        [run_id, run_id, sex],
    )


def _seed_consensus(  # noqa: PLR0913 — schema-aligned positional fields
    conn: DuckDBPyConnection,
    *,
    variant_id: int,
    chrom: str,
    pos: int,
    dosage: int | None,
    is_no_call: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO variants_master (variant_id, rsid, chrom, pos_grch38, ref_allele,
            alt_allele, variant_type)
        VALUES (?, ?, ?::chromosome_enum, ?, 'A', 'G', 'SNV')
        """,
        [variant_id, f"rs{variant_id}", chrom, pos],
    )
    conn.execute(
        """
        INSERT INTO consensus_genotypes (variant_id, consensus_allele_1, consensus_allele_2,
            is_no_call, dosage, consensus_method, resolution_rule)
        VALUES (?, 'A', 'G', ?, ?, 'imputed_only', 'consensus_v1')
        """,
        [variant_id, is_no_call, dosage],
    )


def test_male_profile_corrects_non_par_dosage_and_flags_het_anomaly(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths
) -> None:
    init_databases()  # builds the view via DDL — raises if it doesn't compile
    with duckdb_connection() as conn:
        _insert_chip_qc(conn, 1, "23andme", "M")
        _insert_chip_qc(conn, 2, "ancestry", "ambiguous")  # this user's corpus
        cases = [
            (1, "X", _NONPAR_CORE, 2, False),  # non-PAR hom-alt -> 1
            (2, "X", _NONPAR_CORE + 1, 0, False),  # non-PAR hom-ref -> 0
            (3, "X", _NONPAR_CORE + 2, 1, False),  # non-PAR het -> anomaly, stays 1
            (4, "X", 5_000, 2, False),  # sliver below PAR1 -> non-PAR -> 1
            (5, "X", 156_035_000, 2, False),  # sliver above PAR2 -> non-PAR -> 1
            (6, "X", 1_000_000, 1, False),  # PAR1 het -> passthrough
            (7, "X", 155_800_000, 1, False),  # PAR2 het -> passthrough
            (8, "1", 999, 1, False),  # autosome -> passthrough
            (9, "X", _NONPAR_CORE + 3, None, True),  # non-PAR no-call -> NULL
        ]
        for vid, chrom, pos, dosage, nc in cases:
            _seed_consensus(
                conn, variant_id=vid, chrom=chrom, pos=pos, dosage=dosage, is_no_call=nc
            )

        result = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT variant_id, stored_dosage, corrected_dosage, male_nonpar_het_anomaly "
                "FROM consensus_chrx_dosage_v ORDER BY variant_id",
            ).fetchall()
        }
        anomaly_count = conn.execute(
            "SELECT COUNT(*) FROM consensus_chrx_dosage_v WHERE male_nonpar_het_anomaly",
        ).fetchone()[0]

    assert result[1] == (2, 1, False)  # non-PAR hom-alt corrected 2->1
    assert result[2] == (0, 0, False)  # non-PAR hom-ref stays 0
    assert result[3] == (1, 1, True)  # non-PAR het flagged, dosage untouched
    assert result[4] == (2, 1, False)  # lower sliver corrected
    assert result[5] == (2, 1, False)  # upper sliver corrected
    assert result[6] == (1, 1, False)  # PAR1 passthrough
    assert result[7] == (1, 1, False)  # PAR2 passthrough
    assert result[8] == (1, 1, False)  # autosome passthrough
    assert result[9] == (None, None, None)  # no-call -> NULL corrected
    assert anomaly_count == 1


@pytest.mark.parametrize("sex", ["F", "ambiguous"])
def test_non_male_profile_passes_everything_through(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths
    sex: str,
) -> None:
    """A female or ambiguous profile never halves chrX dosage."""
    init_databases()
    with duckdb_connection() as conn:
        _insert_chip_qc(conn, 1, "23andme", sex)
        _seed_consensus(conn, variant_id=1, chrom="X", pos=_NONPAR_CORE, dosage=2)
        _seed_consensus(conn, variant_id=2, chrom="X", pos=_NONPAR_CORE + 2, dosage=1)
        rows = conn.execute(
            "SELECT stored_dosage, corrected_dosage, male_nonpar_het_anomaly "
            "FROM consensus_chrx_dosage_v ORDER BY variant_id",
        ).fetchall()
    assert rows == [(2, 2, False), (1, 1, False)]


def test_view_non_par_predicate_matches_is_nonpar(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths
) -> None:
    """The view halves a male hom-alt(2) chrX row exactly where ``is_nonpar`` is True."""
    positions = [
        1,
        10_000,
        10_001,
        2_781_479,
        2_781_480,
        _NONPAR_CORE,
        155_701_382,
        155_701_383,
        156_030_895,
        156_030_896,
        156_040_895,
    ]
    init_databases()
    with duckdb_connection() as conn:
        _insert_chip_qc(conn, 1, "23andme", "M")
        for vid, pos in enumerate(positions, start=1):
            _seed_consensus(conn, variant_id=vid, chrom="X", pos=pos, dosage=2)
        rows = conn.execute(
            "SELECT pos_grch38, corrected_dosage FROM consensus_chrx_dosage_v",
        ).fetchall()
    for pos, corrected in rows:
        nonpar = is_nonpar(pos)
        assert corrected == (1 if nonpar else 2), f"pos {pos} corrected={corrected} nonpar={nonpar}"


@pytest.mark.parametrize(
    "qc",
    [
        [("23andme", "M")],
        [("23andme", "M"), ("ancestry", "ambiguous")],
        [("23andme", "F")],
        [("23andme", "ambiguous"), ("ancestry", "ambiguous")],
        [("23andme", "M"), ("ancestry", "F")],  # conflict -> ambiguous
    ],
)
def test_view_profile_sex_parity_with_resolve_sex(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths
    qc: list[tuple[str, str]],
) -> None:
    """The view halves a non-PAR dosage iff the profile resolves to male — the
    same 'M' the Python :func:`profile_sex_label` returns.
    """
    init_databases()
    with duckdb_connection() as conn:
        for i, (source, sex) in enumerate(qc, start=1):
            _insert_chip_qc(conn, i, source, sex)
        _seed_consensus(conn, variant_id=1, chrom="X", pos=_NONPAR_CORE, dosage=2)
        corrected = conn.execute(
            "SELECT corrected_dosage FROM consensus_chrx_dosage_v WHERE variant_id = 1",
        ).fetchone()[0]
        label = profile_sex_label(conn)

    view_treated_as_male = corrected == 1
    assert view_treated_as_male is (label == "M")


def test_materialize_view_recreates_after_drop(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths
) -> None:
    """``materialize_view`` re-creates the view on a live DB without a rebuild."""
    init_databases()
    with duckdb_connection() as conn:
        conn.execute("DROP VIEW consensus_chrx_dosage_v")
        # Materialize it back from the canonical DDL (the no-rebuild path).
        materialize_view(conn, "consensus_chrx_dosage_v")
        _insert_chip_qc(conn, 1, "23andme", "M")
        _seed_consensus(conn, variant_id=1, chrom="X", pos=_NONPAR_CORE, dosage=2)
        corrected = conn.execute(
            "SELECT corrected_dosage FROM consensus_chrx_dosage_v WHERE variant_id = 1",
        ).fetchone()[0]
        # Idempotent: a second materialize is a no-op.
        materialize_view(conn, "consensus_chrx_dosage_v")
    assert corrected == 1
