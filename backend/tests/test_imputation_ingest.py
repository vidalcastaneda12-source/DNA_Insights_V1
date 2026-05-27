"""Tests for :mod:`genome.imputation.ingest` — the import step.

A small synthetic imputed VCF feeds the streaming ingest path; assertions
verify schema-correct writes to ``variants_master``, ``genotype_calls``,
``ingestion_runs``, ``imputation_runs``, and ``sample_qc``.

A separate benchmark test produces a 1 M-row synthetic VCF and ensures the
streaming ingest completes within a generous wall-clock ceiling so the
documented "extrapolates to ~30 min on the real 30 M-row set" claim has
a guard against regression.
"""

from __future__ import annotations

import gzip
import time
from typing import TYPE_CHECKING

import pytest

from genome.db import duckdb_connection, init_databases
from genome.imputation.archive import ImputationArchive
from genome.imputation.ingest import (
    DryRunResult,
    ImportResult,
    import_result,
    parse_chromosomes_filter,
)
from genome.imputation.runs import (
    fetch_run,
    insert_run,
    record_download,
    update_status,
)

if TYPE_CHECKING:
    from pathlib import Path


def _seed_completed_run(*, archive_root: Path | None = None) -> int:
    """Create a fake ``imputation_runs`` row in status='completed'.

    Returns the imputation_id so tests can call import_result against it.
    """
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="beagle",
            reference_panel="1000g_phase3_grch38",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=100,
        )
        update_status(
            conn,
            imp_id,
            status="completed",
            set_submitted=True,
            set_completed=True,
        )
        record_download(
            conn,
            imp_id,
            output_file_path="/tmp/x.zip",  # noqa: S108 — string in test data, not a real path
            output_file_hash_sha256="a" * 64,
        )
    if archive_root is not None:
        ImputationArchive.for_run(archive_root, imp_id).ensure_layout()
    return imp_id


def _write_synthetic_vcf(  # noqa: PLR0913 — direct knobs make per-test variations easy
    dest: Path,
    *,
    chrom: str = "chr1",
    n_variants: int = 5,
    r2_low_count: int = 0,
    r2_high_count: int = 0,
    start_pos: int = 100,
    include_no_call: bool = False,
    r2_info_key: str = "R2",
) -> None:
    """Build a tiny imputed VCF.

    The variants are SNVs at consecutive positions starting at ``start_pos``.
    ``r2_low_count`` of them get R²=0.25; ``r2_high_count`` get R²=0.95; the
    rest get R²=0.5. ``include_no_call=True`` makes the first variant a `./.`
    genotype to exercise the no-call path. ``r2_info_key`` controls which
    INFO key carries the R² value — ``DR2`` is Beagle 5.5's native field,
    ``R2`` matches TopMed-style output, ``Rsq`` matches older Minimac.
    """
    header = (
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID={chrom},length=248956422,assembly=GRCh38>\n"
        f'##INFO=<ID={r2_info_key},Number=1,Type=Float,Description="Imputation R-squared">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
    )

    lines = [header]
    for i in range(n_variants):
        pos = start_pos + i
        if i < r2_low_count:
            r2 = 0.25
        elif i < r2_low_count + r2_high_count:
            r2 = 0.95
        else:
            r2 = 0.5
        rsid = f"rs{i + 1000}"
        ref = "A"
        alt = "G"
        # Cycle through GT shapes so het/hom/no-call all show up.
        if include_no_call and i == 0:
            gt = "./."
        elif i % 3 == 0:
            gt = "0|0"
        elif i % 3 == 1:
            gt = "0|1"
        else:
            gt = "1|1"
        lines.append(
            f"{chrom}\t{pos}\t{rsid}\t{ref}\t{alt}\t.\tPASS\t{r2_info_key}={r2}\tGT\t{gt}\n",
        )
    with gzip.open(dest, "wt", encoding="ascii") as out:
        out.writelines(lines)


def test_import_writes_genotype_calls_with_r2_and_beagle_source(
    isolated_settings: dict[str, str],
    tmp_path: Path,  # noqa: ARG001 — isolated_settings already redirects paths
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)

    # Synthesize chr1 + chrX result files.
    chr1 = archive.result_dir / "chr1.dose.vcf.gz"
    chrx = archive.result_dir / "chrX.dose.vcf.gz"
    _write_synthetic_vcf(chr1, chrom="chr1", n_variants=10, r2_high_count=4)
    _write_synthetic_vcf(chrx, chrom="chrX", n_variants=5)

    result = import_result(imp_id, archive_root=archive_root)

    assert result.variants_total == 15
    assert result.variants_called == 15
    assert result.variants_no_call == 0
    assert result.new_variants_master_rows == 15
    assert result.deactivated_prior_calls == 0
    # The 4 high-R² variants are above 0.8; all 15 are above 0.3.
    assert result.variants_above_r2_0_8 == 4
    assert result.variants_above_r2_0_3 == 15

    with duckdb_connection() as conn:
        gc_rows = conn.execute(
            "SELECT source, is_imputed, imputation_panel, imputation_r2 "
            "FROM genotype_calls WHERE is_active "
            "ORDER BY variant_id",
        ).fetchall()
        master_n = conn.execute(
            "SELECT COUNT(*) FROM variants_master",
        ).fetchone()[0]
        imp_call_count = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls WHERE source='beagle_imputed'",
        ).fetchone()[0]
    assert master_n == 15
    assert imp_call_count == 15
    assert all(r[0] == "beagle_imputed" for r in gc_rows)
    assert all(r[1] for r in gc_rows)  # is_imputed
    assert all(r[2] == "1000g_phase3_grch38" for r in gc_rows)
    assert all(0.0 < r[3] <= 1.0 for r in gc_rows)


def isolated_settings_archive_root(env: dict[str, str]) -> Path:
    from pathlib import Path  # noqa: PLC0415 — keep import out of typing only

    return Path(env["ARCHIVE_PATH"])


def test_import_creates_one_ingestion_run_and_one_sample_qc(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", n_variants=5)

    result = import_result(imp_id, archive_root=archive_root)

    with duckdb_connection() as conn:
        runs = conn.execute(
            "SELECT source, file_native_build, variants_total FROM ingestion_runs WHERE run_id = ?",
            [result.ingestion_run_id],
        ).fetchall()
        qc = conn.execute(
            "SELECT call_rate, sex_inferred, mean_imputation_r2, qc_status"
            " FROM sample_qc WHERE qc_id = ?",
            [result.qc_id],
        ).fetchall()
    assert runs == [("beagle_imputed", "GRCh38", 5)]
    assert qc[0][0] == 1  # call_rate = 1.0 exactly
    assert qc[0][3] in {"pass", "warn", "fail"}
    # The mean R² should be the mean of the synthetic file's R² values (all 0.5
    # since neither r2_low nor r2_high counters were set).
    assert abs(float(qc[0][2]) - 0.5) < 1e-6


def test_import_updates_imputation_runs_volumes(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(
        archive.result_dir / "chr1.dose.vcf.gz",
        n_variants=10,
        r2_low_count=2,
        r2_high_count=3,
    )

    # r2_threshold=0.0 disables the new default filter so the test still
    # exercises the variants_above_r2_0_3 / variants_above_r2_0_8 counters
    # against the full mixed-R² input (a separate test covers the filter).
    import_result(imp_id, archive_root=archive_root, r2_threshold=0.0)

    with duckdb_connection() as conn:
        run = fetch_run(conn, imp_id)
    assert run is not None
    assert run.variants_output == 10
    assert run.variants_above_r2_0_3 == 8  # 10 total - 2 low
    assert run.variants_above_r2_0_8 == 3
    # Mean R² = (2*0.25 + 3*0.95 + 5*0.5) / 10 = 0.585; tolerance accommodates
    # the float32 round-trip through the VCF INFO field.
    assert run.mean_r2 is not None
    assert abs(run.mean_r2 - 0.585) < 1e-6
    assert run.r2_threshold == 0.0


def test_import_raises_when_no_result_vcfs_found(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    # Don't write any result files.
    with pytest.raises(RuntimeError, match="no per-chromosome VCFs"):
        import_result(imp_id, archive_root=archive_root)


def test_import_rejects_unknown_id(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    with pytest.raises(ValueError, match="not found"):
        import_result(999, archive_root=archive_root)


def test_import_rejects_pending_run(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    with duckdb_connection() as conn:
        imp_id = insert_run(
            conn,
            input_run_ids=(1,),
            imputation_server="beagle",
            reference_panel="1000g_phase3_grch38",
            pipeline_version="imputation_prepare_v0.1.0",
            variants_input=10,
        )
    with pytest.raises(RuntimeError, match="download the result first"):
        import_result(imp_id, archive_root=archive_root)


def test_import_handles_no_call_genotypes(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(
        archive.result_dir / "chr1.dose.vcf.gz",
        n_variants=5,
        include_no_call=True,
    )

    result = import_result(imp_id, archive_root=archive_root)
    assert result.variants_no_call == 1
    assert result.variants_called == 4


def test_reimport_supersedes_prior_imputed_calls(
    isolated_settings: dict[str, str],
) -> None:
    """A second import on the same run deactivates the first import's calls."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", n_variants=5)

    first = import_result(imp_id, archive_root=archive_root)
    assert first.deactivated_prior_calls == 0

    # Re-running against an already-imported run now requires --force-reimport;
    # the supersession-over-update semantics are unchanged.
    second = import_result(imp_id, archive_root=archive_root, force_reimport=True)
    assert second.deactivated_prior_calls == 5

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls WHERE is_active",
        ).fetchone()[0]
        inactive = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls WHERE NOT is_active",
        ).fetchone()[0]
    assert active == 5
    assert inactive == 5  # five from the first import are deactivated


def test_explicit_paths_override_archive_layout(
    isolated_settings: dict[str, str],
    tmp_path: Path,
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    # Place the VCF anywhere — explicit_vcf_paths drives import.
    elsewhere = tmp_path / "chr1.dose.vcf.gz"
    _write_synthetic_vcf(elsewhere, n_variants=3)

    result = import_result(
        imp_id,
        archive_root=archive_root,
        explicit_vcf_paths=(elsewhere,),
    )
    assert result.variants_total == 3


def test_benchmark_streams_1m_rows_within_60s(
    isolated_settings: dict[str, str],
    tmp_path: Path,  # noqa: ARG001 — fixture forces a tmp-scoped settings root
) -> None:
    """1M-row imputed VCF streams in under 60 seconds.

    A 1M-row benchmark gives a useful scaling guide for the full imputed
    set (several million variants). Real-world numbers from the ingest path:
    on a development machine, 1M rows clear in well under 10 seconds; the
    60-second ceiling is generous slack to survive CI variability.
    """
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    target = archive.result_dir / "chr1.dose.vcf.gz"
    _write_synthetic_vcf(target, n_variants=1_000_000)

    start = time.monotonic()
    result = import_result(imp_id, archive_root=archive_root)
    elapsed = time.monotonic() - start

    assert result.variants_total == 1_000_000
    assert elapsed < 60.0, f"1M-row import took {elapsed:.1f}s, expected < 60s"


# ---------------------------------------------------------------------------
# Phase 4 follow-up: operational controls on `genome imputation import`.
# ---------------------------------------------------------------------------


def test_r2_threshold_filters_low_confidence_variants(
    isolated_settings: dict[str, str],
) -> None:
    """Variants with INFO/R2 below ``r2_threshold`` are skipped at import."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    # 10 variants total: 2 at R²=0.25 (below 0.3), 3 at R²=0.95, 5 at R²=0.5.
    _write_synthetic_vcf(
        archive.result_dir / "chr1.dose.vcf.gz",
        n_variants=10,
        r2_low_count=2,
        r2_high_count=3,
    )

    result = import_result(imp_id, archive_root=archive_root, r2_threshold=0.3)
    assert isinstance(result, ImportResult)
    assert result.variants_total == 8
    assert result.variants_below_threshold == 2
    assert result.r2_threshold == 0.3

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls WHERE is_active",
        ).fetchone()[0]
        run = fetch_run(conn, imp_id)
    assert active == 8
    assert run is not None
    assert run.r2_threshold == 0.3


def test_r2_threshold_zero_lets_every_variant_through(
    isolated_settings: dict[str, str],
) -> None:
    """``r2_threshold=0.0`` reproduces the pre-flag behavior."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(
        archive.result_dir / "chr1.dose.vcf.gz",
        n_variants=10,
        r2_low_count=4,
    )
    result = import_result(imp_id, archive_root=archive_root, r2_threshold=0.0)
    assert isinstance(result, ImportResult)
    assert result.variants_total == 10
    assert result.variants_below_threshold == 0


def test_import_reads_beagle_native_dr2_info_field(
    isolated_settings: dict[str, str],
) -> None:
    """Beagle 5.5 emits INFO/DR2; the importer must read it natively."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    # 10 variants total, all carrying INFO/DR2 instead of R2: 2 at DR²=0.25
    # (below 0.3), 3 at DR²=0.95, 5 at DR²=0.5.
    _write_synthetic_vcf(
        archive.result_dir / "chr1.dose.vcf.gz",
        n_variants=10,
        r2_low_count=2,
        r2_high_count=3,
        r2_info_key="DR2",
    )

    result = import_result(imp_id, archive_root=archive_root, r2_threshold=0.3)
    assert isinstance(result, ImportResult)
    assert result.variants_total == 8
    assert result.variants_below_threshold == 2
    # mean_r2 should reflect the parsed DR2 values: (3*0.95 + 5*0.5) / 8.
    assert result.mean_r2 is not None
    assert abs(result.mean_r2 - (3 * 0.95 + 5 * 0.5) / 8) < 1e-6


def test_r2_threshold_validates_range(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", n_variants=1)
    with pytest.raises(ValueError, match="r2_threshold"):
        import_result(imp_id, archive_root=archive_root, r2_threshold=1.5)


def test_chromosomes_filter_limits_imported_files(
    isolated_settings: dict[str, str],
) -> None:
    """``--chromosomes`` keeps only the requested per-chromosome VCFs."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", chrom="chr1", n_variants=4)
    _write_synthetic_vcf(archive.result_dir / "chr2.dose.vcf.gz", chrom="chr2", n_variants=3)
    _write_synthetic_vcf(archive.result_dir / "chrX.dose.vcf.gz", chrom="chrX", n_variants=2)

    result = import_result(
        imp_id,
        archive_root=archive_root,
        chromosomes=frozenset({"1", "X"}),
    )
    assert isinstance(result, ImportResult)
    assert result.variants_total == 6  # chr1 (4) + chrX (2); chr2 was skipped
    assert set(result.chromosomes_imported) == {"1", "X"}

    with duckdb_connection() as conn:
        chroms = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT CAST(chrom AS VARCHAR) FROM variants_master",
            ).fetchall()
        }
    assert chroms == {"1", "X"}


def test_chromosomes_filter_with_no_matching_files_raises(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", chrom="chr1", n_variants=2)
    with pytest.raises(RuntimeError, match="chromosome filter"):
        import_result(
            imp_id,
            archive_root=archive_root,
            chromosomes=frozenset({"22"}),
        )


def test_parse_chromosomes_filter_accepts_chr_prefix_and_lowercase() -> None:
    assert parse_chromosomes_filter("1,2,X") == frozenset({"1", "2", "X"})
    assert parse_chromosomes_filter("chr1, chrX , 22") == frozenset({"1", "X", "22"})
    assert parse_chromosomes_filter(None) is None


def test_parse_chromosomes_filter_rejects_invalid_chromosomes() -> None:
    with pytest.raises(ValueError, match="invalid chromosome"):
        parse_chromosomes_filter("1,FOO")
    with pytest.raises(ValueError, match="empty after parsing"):
        parse_chromosomes_filter(",,")


def test_dry_run_does_not_write_to_database(
    isolated_settings: dict[str, str],
) -> None:
    """``--dry-run`` reports per-chrom counts and skips every DB write."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(
        archive.result_dir / "chr1.dose.vcf.gz",
        chrom="chr1",
        n_variants=10,
        r2_low_count=3,
    )
    _write_synthetic_vcf(
        archive.result_dir / "chr2.dose.vcf.gz",
        chrom="chr2",
        n_variants=4,
    )

    result = import_result(imp_id, archive_root=archive_root, dry_run=True)
    assert isinstance(result, DryRunResult)
    assert result.r2_threshold == 0.3
    assert result.variants_total == 11  # 10 - 3 (below 0.3) + 4
    assert result.variants_below_threshold == 3
    assert result.per_chrom == {"1": 7, "2": 4}
    assert set(result.chromosomes_planned) == {"1", "2"}
    assert result.estimated_seconds > 0

    # Database must still be empty (no calls, no master rows beyond seed).
    with duckdb_connection() as conn:
        gc = conn.execute("SELECT COUNT(*) FROM genotype_calls").fetchone()[0]
        master = conn.execute("SELECT COUNT(*) FROM variants_master").fetchone()[0]
        run = fetch_run(conn, imp_id)
    assert gc == 0
    assert master == 0
    assert run is not None
    assert run.variants_output is None  # not marked as imported
    assert run.r2_threshold is None


def test_dry_run_does_not_check_force_reimport(
    isolated_settings: dict[str, str],
) -> None:
    """Dry-run is read-only, so the already-imported guard must not fire."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", n_variants=3)
    # First do a real import; that lands variants_output.
    import_result(imp_id, archive_root=archive_root)
    # A dry-run after the real import must not require --force-reimport.
    result = import_result(imp_id, archive_root=archive_root, dry_run=True)
    assert isinstance(result, DryRunResult)


def test_force_reimport_required_after_first_import(
    isolated_settings: dict[str, str],
) -> None:
    """A second import without ``--force-reimport`` aborts with a clear error."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", n_variants=5)

    first = import_result(imp_id, archive_root=archive_root)
    assert isinstance(first, ImportResult)

    with pytest.raises(RuntimeError, match="already been imported"):
        import_result(imp_id, archive_root=archive_root)


def test_force_reimport_supersedes_prior_calls(
    isolated_settings: dict[str, str],
) -> None:
    """With ``--force-reimport`` set, the supersession path runs as before."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", n_variants=5)

    import_result(imp_id, archive_root=archive_root)
    second = import_result(imp_id, archive_root=archive_root, force_reimport=True)
    assert isinstance(second, ImportResult)
    assert second.deactivated_prior_calls == 5

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls WHERE is_active",
        ).fetchone()[0]
        inactive = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls WHERE NOT is_active",
        ).fetchone()[0]
    assert active == 5
    assert inactive == 5


def test_chromosomes_filter_allows_adding_chromosomes_without_force(
    isolated_settings: dict[str, str],
) -> None:
    """Partial re-import with ``--chromosomes`` is allowed after a prior import.

    The spec's resume message points the user at ``--chromosomes`` as an
    alternative to ``--force-reimport``. The chromosome filter limits the
    write set, but the guard still applies because re-running on the same
    chromosome would still need a force-reimport — only the variants we
    haven't imported yet should be allowed. For now this test pins the
    behavior of partial re-import requiring ``--force-reimport`` so the user
    explicitly acknowledges the supersession of any overlapping calls.
    """
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", n_variants=3)
    _write_synthetic_vcf(archive.result_dir / "chr2.dose.vcf.gz", chrom="chr2", n_variants=2)
    # First import: chr1 only.
    first = import_result(
        imp_id,
        archive_root=archive_root,
        chromosomes=frozenset({"1"}),
    )
    assert isinstance(first, ImportResult)
    assert first.variants_total == 3

    # Without --force-reimport, partial re-import is still blocked.
    with pytest.raises(RuntimeError, match="already been imported"):
        import_result(
            imp_id,
            archive_root=archive_root,
            chromosomes=frozenset({"2"}),
        )

    # With --force-reimport, chr2 is brought in.
    second = import_result(
        imp_id,
        archive_root=archive_root,
        chromosomes=frozenset({"2"}),
        force_reimport=True,
    )
    assert isinstance(second, ImportResult)
    assert second.variants_total == 2
    assert set(second.chromosomes_imported) == {"2"}


def test_batch_size_does_not_change_output(
    isolated_settings: dict[str, str],
) -> None:
    """Tuning ``--batch-size`` is purely an internal knob; counts must match."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", n_variants=20)

    result = import_result(imp_id, archive_root=archive_root, batch_size=7)
    assert isinstance(result, ImportResult)
    assert result.variants_total == 20

    with duckdb_connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM genotype_calls WHERE is_active",
        ).fetchone()[0]
    assert active == 20


def test_batch_size_rejects_non_positive_values(
    isolated_settings: dict[str, str],
) -> None:
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)
    _write_synthetic_vcf(archive.result_dir / "chr1.dose.vcf.gz", n_variants=1)
    with pytest.raises(ValueError, match="batch_size"):
        import_result(imp_id, archive_root=archive_root, batch_size=0)


# ---------------------------------------------------------------------------
# Pre-Phase-6: truncated-BGZF import guard (finding-008 #2).
# ---------------------------------------------------------------------------


def _write_bgzf_vcf(dest: Path, *, chrom: str = "chrX", n_variants: int = 3) -> None:
    """Write a real BGZF (not plain-gzip) imputed VCF, EOF marker included.

    Beagle's output is BGZF; the plain-``gzip.open`` path in
    :func:`_write_synthetic_vcf` is fine for the parsing tests but cannot
    exercise the BGZF-truncation guard. Build the records with that helper, then
    transcode through ``cyvcf2.Writer(mode="wz")`` so htslib appends the
    canonical 28-byte BGZF EOF marker.
    """
    import cyvcf2  # noqa: PLC0415 — deferred; mirrors the production import site

    plain = dest.parent / f"{dest.name}.plain.vcf.gz"
    _write_synthetic_vcf(plain, chrom=chrom, n_variants=n_variants)
    reader = cyvcf2.VCF(str(plain))
    writer = cyvcf2.Writer(str(dest), reader, mode="wz")
    try:
        for v in reader:
            writer.write_record(v)
    finally:
        writer.close()
        reader.close()
    plain.unlink()


def test_import_raises_on_truncated_bgzf_result_vcf(
    isolated_settings: dict[str, str],
) -> None:
    """A truncated BGZF result VCF (missing its EOF marker) aborts the import.

    Reproduces finding-008 #2: Beagle dies mid-write on chrX, leaving a BGZF
    file with no EOF marker; cyvcf2 reads it as zero variants without raising.
    The guard must turn that silent success into a loud failure.
    """
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)

    chrx = archive.result_dir / "chrX.dose.vcf.gz"
    _write_bgzf_vcf(chrx, chrom="chrX", n_variants=3)
    # Drop the trailing 28-byte BGZF EOF marker to mimic the truncated write.
    chrx.write_bytes(chrx.read_bytes()[:-28])

    with pytest.raises(RuntimeError, match="truncated BGZF"):
        import_result(imp_id, archive_root=archive_root)


def test_import_accepts_intact_bgzf_result_vcf(
    isolated_settings: dict[str, str],
) -> None:
    """An intact BGZF result VCF imports normally.

    Confirms the guard keys on the EOF marker, not the gzip flavor: a complete
    BGZF file (marker present) passes, so real Beagle output that finished
    cleanly is accepted and the plain-gzip fixtures elsewhere stay exempt.
    """
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)

    chr1 = archive.result_dir / "chr1.dose.vcf.gz"
    _write_bgzf_vcf(chr1, chrom="chr1", n_variants=4)

    result = import_result(imp_id, archive_root=archive_root)
    assert isinstance(result, ImportResult)
    assert result.variants_total == 4


def test_dry_run_also_rejects_truncated_bgzf_result_vcf(
    isolated_settings: dict[str, str],
) -> None:
    """The truncation guard also fires on the dry-run path (shared open helper)."""
    init_databases()
    archive_root = isolated_settings_archive_root(isolated_settings)
    imp_id = _seed_completed_run(archive_root=archive_root)
    archive = ImputationArchive.for_run(archive_root, imp_id)

    chrx = archive.result_dir / "chrX.dose.vcf.gz"
    _write_bgzf_vcf(chrx, chrom="chrX", n_variants=3)
    chrx.write_bytes(chrx.read_bytes()[:-28])

    with pytest.raises(RuntimeError, match="truncated BGZF"):
        import_result(imp_id, archive_root=archive_root, dry_run=True)
