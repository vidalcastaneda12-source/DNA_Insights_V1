"""Tests for the post-merge male non-PAR chrX het guard (PR 5a, finding-029).

Covers :func:`genome.merge.chrx_qc.apply_chrx_het_guard` directly and end-to-end
through :func:`genome.merge.merge_all`: the anomaly count, the idempotent
``[chrx_male_nonpar_het=N]`` marker on the imputed ``sample_qc.qc_notes``, marker
clearing when the count returns to zero, and preservation of existing notes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from genome.db import duckdb_connection, init_databases
from genome.merge import merge_all
from genome.merge.chrx_qc import apply_chrx_het_guard

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

_NONPAR_CORE = 50_000_000


def _insert_chip_qc(conn: DuckDBPyConnection, run_id: int, sex: str) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_runs (run_id, source, file_path, file_hash_sha256,
            status, pipeline_version)
        VALUES (?, '23andme', ?, ?, 'completed', 'test')
        """,
        [run_id, f"/t/{run_id}", "0" * 64],
    )
    conn.execute(
        "INSERT INTO sample_qc (qc_id, run_id, sex_inferred, qc_status) VALUES (?, ?, ?, 'pass')",
        [run_id, run_id, sex],
    )


def _insert_imputed_qc(
    conn: DuckDBPyConnection,
    run_id: int,
    *,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_runs (run_id, source, file_path, file_hash_sha256,
            status, pipeline_version)
        VALUES (?, 'beagle_imputed', ?, ?, 'completed', 'test')
        """,
        [run_id, f"/t/{run_id}", "0" * 64],
    )
    conn.execute(
        "INSERT INTO sample_qc (qc_id, run_id, sex_inferred, qc_status, qc_notes) "
        "VALUES (?, ?, 'M', 'pass', ?)",
        [run_id, run_id, notes],
    )


def _insert_consensus(
    conn: DuckDBPyConnection,
    variant_id: int,
    pos: int,
    dosage: int,
) -> None:
    conn.execute(
        """
        INSERT INTO variants_master (variant_id, rsid, chrom, pos_grch38, ref_allele,
            alt_allele, variant_type)
        VALUES (?, ?, 'X', ?, 'A', 'G', 'SNV')
        """,
        [variant_id, f"rs{variant_id}", pos],
    )
    conn.execute(
        """
        INSERT INTO consensus_genotypes (variant_id, consensus_allele_1, consensus_allele_2,
            is_no_call, dosage, consensus_method, resolution_rule)
        VALUES (?, 'A', 'G', FALSE, ?, 'imputed_only', 'consensus_v1')
        """,
        [variant_id, dosage],
    )


def test_het_guard_counts_and_annotates_idempotently(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        _insert_chip_qc(conn, 1, "M")
        _insert_imputed_qc(conn, 2)
        _insert_consensus(conn, 1, _NONPAR_CORE, dosage=1)  # male non-PAR het -> anomaly
        _insert_consensus(conn, 2, _NONPAR_CORE + 1, dosage=2)  # non-PAR hom-alt -> fine

        assert apply_chrx_het_guard(conn) == 1
        notes = conn.execute("SELECT qc_notes FROM sample_qc WHERE qc_id = 2").fetchone()[0]
        assert "[chrx_male_nonpar_het=1]" in notes

        # Re-running is idempotent — no stacked markers.
        assert apply_chrx_het_guard(conn) == 1
        notes_again = conn.execute("SELECT qc_notes FROM sample_qc WHERE qc_id = 2").fetchone()[0]
        assert notes_again == notes


def test_het_guard_clears_marker_when_resolved(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        _insert_chip_qc(conn, 1, "M")
        _insert_imputed_qc(conn, 2, notes="call_rate=1.0000")
        _insert_consensus(conn, 1, _NONPAR_CORE, dosage=1)

        apply_chrx_het_guard(conn)
        notes = conn.execute("SELECT qc_notes FROM sample_qc WHERE qc_id = 2").fetchone()[0]
        assert "call_rate=1.0000" in notes  # pre-existing note preserved
        assert "[chrx_male_nonpar_het=1]" in notes

        # Resolve the anomaly (dosage 1 -> 2) and re-run: marker cleared, note kept.
        conn.execute("UPDATE consensus_genotypes SET dosage = 2 WHERE variant_id = 1")
        assert apply_chrx_het_guard(conn) == 0
        cleared = conn.execute("SELECT qc_notes FROM sample_qc WHERE qc_id = 2").fetchone()[0]
        assert "chrx_male_nonpar_het" not in cleared
        assert cleared == "call_rate=1.0000"


def test_het_guard_zero_for_female_profile_no_crash(
    isolated_settings: dict[str, str],  # noqa: ARG001 — redirects DB paths
) -> None:
    """A female profile yields no anomaly even with a non-PAR het consensus row."""
    init_databases()
    with duckdb_connection() as conn:
        _insert_chip_qc(conn, 1, "F")
        _insert_imputed_qc(conn, 2)
        _insert_consensus(conn, 1, _NONPAR_CORE, dosage=1)
        assert apply_chrx_het_guard(conn) == 0
        notes = conn.execute("SELECT qc_notes FROM sample_qc WHERE qc_id = 2").fetchone()[0]
        assert notes is None


def test_merge_all_records_chrx_het_anomaly(
    isolated_settings: dict[str, str],
) -> None:
    """End-to-end: merge_all rebuilds consensus and the guard stamps the QC row."""
    init_databases()
    db = Path(isolated_settings["GENOME_DUCKDB_PATH"])
    with duckdb_connection(db) as conn:
        _insert_chip_qc(conn, 1, "M")  # male profile
        _insert_imputed_qc(conn, 3)  # the annotation target (imputed run)
        # A non-PAR chrX variant with an active imputed HET call → imputed_only
        # het consensus (dosage 1) → male non-PAR het anomaly after merge.
        conn.execute(
            """
            INSERT INTO variants_master (variant_id, rsid, chrom, pos_grch38, ref_allele,
                alt_allele, variant_type)
            VALUES (1, 'rs1', 'X', ?, 'A', 'G', 'SNV')
            """,
            [_NONPAR_CORE],
        )
        conn.execute(
            """
            INSERT INTO genotype_calls (call_id, variant_id, source, ingestion_run_id,
                genotype_raw, allele_1, allele_2, is_no_call, is_imputed, imputation_r2,
                imputation_panel, raw_strand, strand_status, is_active)
            VALUES (1, 1, 'beagle_imputed', 3, 'AG', 'A', 'G', FALSE, TRUE, 0.9,
                '1000g_phase3_grch38', '+', 'resolved_plus', TRUE)
            """,
        )

    merge_all(duckdb_path=db)

    with duckdb_connection(db) as conn:
        notes = conn.execute("SELECT qc_notes FROM sample_qc WHERE qc_id = 3").fetchone()[0]
        anomaly = conn.execute(
            "SELECT COUNT(*) FROM consensus_chrx_dosage_v WHERE male_nonpar_het_anomaly",
        ).fetchone()[0]
    assert anomaly == 1
    assert notes is not None
    assert "[chrx_male_nonpar_het=1]" in notes


def test_merge_chrx_chip_plus_imputed_append_unchanged(
    isolated_settings: dict[str, str],
) -> None:
    """A chrX chip call with a confirming imputed call resolves chip-first (PR 5a).

    The consensus rule is chromosome-agnostic, so chrX must behave exactly like
    an autosome: the chip resolution stands and the imputed call is appended to
    ``contributing_calls`` as confirming evidence. Uses a PAR1 het so the het
    guard stays out of the picture.
    """
    init_databases()
    db = Path(isolated_settings["GENOME_DUCKDB_PATH"])
    par1_pos = 1_000_000
    with duckdb_connection(db) as conn:
        _insert_chip_qc(conn, 1, "M")
        _insert_imputed_qc(conn, 3)
        conn.execute(
            """
            INSERT INTO variants_master (variant_id, rsid, chrom, pos_grch38, ref_allele,
                alt_allele, variant_type)
            VALUES (1, 'rs1', 'X', ?, 'A', 'G', 'SNV')
            """,
            [par1_pos],
        )
        # 23andme het + a confirming imputed het at the same chrX (PAR1) site.
        conn.execute(
            """
            INSERT INTO genotype_calls (call_id, variant_id, source, ingestion_run_id,
                genotype_raw, allele_1, allele_2, is_no_call, is_imputed, imputation_r2,
                imputation_panel, raw_strand, strand_status, is_active)
            VALUES (10, 1, '23andme', 1, 'AG', 'A', 'G', FALSE, FALSE, NULL,
                NULL, '+', 'resolved_plus', TRUE)
            """,
        )
        conn.execute(
            """
            INSERT INTO genotype_calls (call_id, variant_id, source, ingestion_run_id,
                genotype_raw, allele_1, allele_2, is_no_call, is_imputed, imputation_r2,
                imputation_panel, raw_strand, strand_status, is_active)
            VALUES (11, 1, 'beagle_imputed', 3, 'AG', 'A', 'G', FALSE, TRUE, 0.95,
                '1000g_phase3_grch38', '+', 'resolved_plus', TRUE)
            """,
        )

    merge_all(duckdb_path=db)

    with duckdb_connection(db) as conn:
        method, contributing, dosage = conn.execute(
            "SELECT consensus_method, contributing_calls, dosage "
            "FROM consensus_genotypes WHERE variant_id = 1",
        ).fetchone()
        anomaly = conn.execute(
            "SELECT COUNT(*) FROM consensus_chrx_dosage_v WHERE male_nonpar_het_anomaly",
        ).fetchone()[0]

    assert method == "single_source"  # one chip platform; imputed is confirming only
    assert set(contributing) == {10, 11}  # imputed call appended
    assert dosage == 1  # A/G het, ALT=G
    assert anomaly == 0  # PAR1 het is legitimate — not flagged
