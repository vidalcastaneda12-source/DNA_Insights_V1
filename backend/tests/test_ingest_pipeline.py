"""End-to-end ingest pipeline against fixture files.

Verifies the Phase 2 deliverable: ingest both fixture files, ``variants_master``
populated, ``sample_qc`` row produced, file archived, re-ingest deactivates
prior calls, and the discrepancy-summary view returns rows once both sources
are present.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from genome.cli import app
from genome.db import duckdb_connection, init_databases
from genome.ingest import PIPELINE_VERSION, ingest_file
from genome.ingest.liftover import IdentityLiftover

FIXTURES = Path(__file__).parent / "fixtures"
TWENTYTHREE = FIXTURES / "23andme_sample.txt"
ANCESTRY = FIXTURES / "ancestry_sample.txt"


def _duckdb_path(env: dict[str, str]) -> Path:
    return Path(env["GENOME_DUCKDB_PATH"])


def test_ingest_23andme_fixture_populates_master_and_qc(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    result = ingest_file(source="23andme", path=TWENTYTHREE)

    assert result.run_id == 1
    assert result.qc_id == 1
    assert result.file_native_build == "GRCh38"
    assert result.variants_total == 30
    assert result.variants_no_call == 1
    assert result.variants_called == 29
    assert result.new_variants_master_rows >= 28  # palindromes + indels included
    assert result.deactivated_prior_calls == 0
    # Y chrom calls present → male inferred.
    assert result.sex_inferred == "M"

    with duckdb_connection(_duckdb_path(isolated_settings), read_only=True) as conn:
        master_n = conn.execute("SELECT COUNT(*) FROM variants_master").fetchone()[0]
        gc_n = conn.execute("SELECT COUNT(*) FROM genotype_calls").fetchone()[0]
        runs_n = conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]
        qc_n = conn.execute("SELECT COUNT(*) FROM sample_qc").fetchone()[0]
        run = conn.execute(
            "SELECT source, file_native_build, status, variants_total, pipeline_version"
            " FROM ingestion_runs WHERE run_id = ?",
            [result.run_id],
        ).fetchone()
    assert master_n >= 28
    assert gc_n == 30
    assert runs_n == 1
    assert qc_n == 1
    assert run == ("23andme", "GRCh38", "completed", 30, PIPELINE_VERSION)


def test_ingest_archives_file(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture sets env
) -> None:
    init_databases()
    result = ingest_file(source="23andme", path=TWENTYTHREE)
    assert result.archived_path.is_file()
    assert result.archived_path.parent.name == "23andme"
    assert result.file_hash_sha256 in result.archived_path.name
    assert (result.archived_path.stat().st_mode & 0o777) == 0o600


def test_ingest_palindrome_and_indel_strand_marking(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    ingest_file(source="23andme", path=TWENTYTHREE)
    with duckdb_connection(_duckdb_path(isolated_settings), read_only=True) as conn:
        # rs6671356 is A/T (palindrome) in the 23andMe fixture.
        palindrome = conn.execute(
            "SELECT strand_status FROM genotype_calls gc"
            " JOIN variants_master vm ON gc.variant_id = vm.variant_id"
            " WHERE vm.rsid = 'rs6671356'",
        ).fetchone()
        # 23andMe-style indel rows.
        indel = conn.execute(
            "SELECT strand_status, vm.variant_type FROM genotype_calls gc"
            " JOIN variants_master vm ON gc.variant_id = vm.variant_id"
            " WHERE vm.rsid = 'i5000001'",
        ).fetchone()
        # The no-call row should still be a genotype_calls row with is_no_call true.
        no_call = conn.execute(
            "SELECT is_no_call FROM genotype_calls gc"
            " JOIN variants_master vm ON gc.variant_id = vm.variant_id"
            " WHERE vm.rsid = 'rs1000999'",
        ).fetchone()
    assert palindrome == ("ambiguous_palindrome",)
    assert indel == ("unknown", "INDEL")
    assert no_call == (True,)


def test_ingest_ancestry_with_identity_liftover_and_dual_source_view(
    isolated_settings: dict[str, str],
) -> None:
    """Both fixtures land; concordance view returns rows for the (23andme,ancestry) pair."""
    init_databases()
    ingest_file(source="23andme", path=TWENTYTHREE)
    ancestry_result = ingest_file(
        source="ancestry",
        path=ANCESTRY,
        liftover=IdentityLiftover(chain_label="hg19_to_hg38"),
    )
    assert ancestry_result.run_id == 2
    assert ancestry_result.file_native_build == "GRCh37"
    assert ancestry_result.variants_total == 20

    with duckdb_connection(_duckdb_path(isolated_settings), read_only=True) as conn:
        # The chrom aliases in Ancestry (23/24/26) must be resolved to X/Y/MT.
        x_rows = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls gc"
            " JOIN variants_master vm ON gc.variant_id = vm.variant_id"
            " WHERE gc.source = 'ancestry' AND vm.chrom = 'X'",
        ).fetchone()[0]
        y_rows = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls gc"
            " JOIN variants_master vm ON gc.variant_id = vm.variant_id"
            " WHERE gc.source = 'ancestry' AND vm.chrom = 'Y'",
        ).fetchone()[0]
        mt_rows = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls gc"
            " JOIN variants_master vm ON gc.variant_id = vm.variant_id"
            " WHERE gc.source = 'ancestry' AND vm.chrom = 'MT'",
        ).fetchone()[0]
        # Concordance summary view should now return at least one (a,b) pair.
        summary_rows = conn.execute("SELECT * FROM concordance_summary_v").fetchall()
        # Platform coverage view should reflect two sources for shared rsids.
        shared_in_both = conn.execute(
            "SELECT COUNT(*) FROM platform_coverage_v WHERE in_23andme AND in_ancestry",
        ).fetchone()[0]
    assert x_rows == 2
    assert y_rows == 6
    assert mt_rows == 2
    assert summary_rows  # at least one comparison pair landed
    assert shared_in_both >= 5  # several rsids overlap across the two fixtures


def test_reingest_deactivates_prior_calls(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    ingest_file(source="23andme", path=TWENTYTHREE)
    second = ingest_file(source="23andme", path=TWENTYTHREE)

    assert second.run_id == 2
    # Every call from run 1 should be deactivated when run 2 lands.
    assert second.deactivated_prior_calls >= 1

    with duckdb_connection(_duckdb_path(isolated_settings), read_only=True) as conn:
        active_per_source = dict(
            conn.execute(
                "SELECT source, COUNT(*) FROM genotype_calls WHERE is_active GROUP BY source",
            ).fetchall(),
        )
        deactivated = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls WHERE NOT is_active",
        ).fetchone()[0]
    assert active_per_source["23andme"] == 30
    assert deactivated == 30


def test_ingest_cli_runs_end_to_end(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture sets env
) -> None:
    init_databases()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ingest", "--source", "23andme", str(TWENTYTHREE)],
    )
    assert result.exit_code == 0, result.output
    assert "run_id=1" in result.output
    assert "qc_id=1" in result.output
    assert "variants=30" in result.output


def test_ingest_cli_rejects_unknown_source(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "--source", "snpedia", str(TWENTYTHREE)])
    assert result.exit_code != 0


def test_ingest_grch37_without_chain_file_errors(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture sets env
) -> None:
    init_databases()
    with pytest.raises(ValueError, match="chain file"):
        ingest_file(source="ancestry", path=ANCESTRY)


def test_ingest_records_alt_contig_drops(
    isolated_settings: dict[str, str],
    tmp_path: Path,
) -> None:
    """A 23andMe v5 file with alt-contig rows ingests cleanly with the count recorded.

    Reproduces the failure mode where ``8_KI270821v1_alt`` reached the DuckDB
    cast and exploded. After the parser-layer filter, the row never lands in
    ``variants_master``; the per-run count surfaces on ``ingestion_runs``.
    """
    init_databases()
    # Build a small file by copying the fixture and inserting alt-contig rows.
    body = TWENTYTHREE.read_text()
    augmented = body.rstrip("\n") + (
        "\ni6045465\t8_KI270821v1_alt\t12345\tAG\ni6045466\t19_KI270938v1_alt\t67890\tCT\n"
    )
    p = tmp_path / "23andme_v5_with_alt.txt"
    p.write_text(augmented)

    result = ingest_file(source="23andme", path=p)

    # Two rows filtered at parse time; the rest of the file ingests cleanly.
    assert result.variants_dropped_alt_contig == 2
    assert result.variants_total == 30  # original fixture row count
    assert result.qc_status in {"pass", "warn", "fail"}

    with duckdb_connection(_duckdb_path(isolated_settings), read_only=True) as conn:
        run = conn.execute(
            "SELECT variants_total, variants_dropped_alt_contig"
            " FROM ingestion_runs WHERE run_id = ?",
            [result.run_id],
        ).fetchone()
        # No alt-contig variant should have made it to variants_master.
        leaked = conn.execute(
            "SELECT COUNT(*) FROM variants_master vm"
            " WHERE CAST(vm.chrom AS VARCHAR) NOT IN"
            " ('1','2','3','4','5','6','7','8','9','10',"
            "  '11','12','13','14','15','16','17','18','19','20',"
            "  '21','22','X','Y','MT')",
        ).fetchone()
    assert run == (30, 2)
    assert leaked == (0,)
