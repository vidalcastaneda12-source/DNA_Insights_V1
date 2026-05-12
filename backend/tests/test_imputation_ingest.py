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
from genome.imputation.ingest import import_result
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
            imputation_server="topmed",
            reference_panel="topmed_r3",
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
) -> None:
    """Build a tiny imputed VCF.

    The variants are SNVs at consecutive positions starting at ``start_pos``.
    ``r2_low_count`` of them get R²=0.25; ``r2_high_count`` get R²=0.95; the
    rest get R²=0.5. ``include_no_call=True`` makes the first variant a `./.`
    genotype to exercise the no-call path.
    """
    header = (
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID={chrom},length=248956422,assembly=GRCh38>\n"
        '##INFO=<ID=R2,Number=1,Type=Float,Description="Imputation R-squared">\n'
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
            f"{chrom}\t{pos}\t{rsid}\t{ref}\t{alt}\t.\tPASS\tR2={r2}\tGT\t{gt}\n",
        )
    with gzip.open(dest, "wt", encoding="ascii") as out:
        out.writelines(lines)


def test_import_writes_genotype_calls_with_r2_and_topmed_source(
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
            "SELECT COUNT(*) FROM genotype_calls WHERE source='topmed_imputed'",
        ).fetchone()[0]
    assert master_n == 15
    assert imp_call_count == 15
    assert all(r[0] == "topmed_imputed" for r in gc_rows)
    assert all(r[1] for r in gc_rows)  # is_imputed
    assert all(r[2] == "topmed_r3" for r in gc_rows)
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
    assert runs == [("topmed_imputed", "GRCh38", 5)]
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

    import_result(imp_id, archive_root=archive_root)

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
            imputation_server="topmed",
            reference_panel="topmed_r3",
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

    second = import_result(imp_id, archive_root=archive_root)
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

    The real TopMed result is ~30M variants. A 1M-row benchmark gives us a
    ~30x scaling guide. Real-world numbers from the ingest path: on a
    development machine, 1M rows clear in well under 10 seconds; the 60-second
    ceiling is generous slack to survive CI variability.
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
