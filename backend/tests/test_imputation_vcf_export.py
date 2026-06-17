"""Tests for :mod:`genome.imputation.vcf_export` — the prepare step."""

from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING

import pytest

from genome.db import duckdb_connection, init_databases
from genome.imputation.vcf_export import (
    EXPORT_PIPELINE_VERSION,
    prepare_run,
)

if TYPE_CHECKING:
    from pathlib import Path

    from duckdb import DuckDBPyConnection


def _seed_ingestion_run(conn: DuckDBPyConnection, run_id: int, source: str) -> None:
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
        [run_id, source, f"/test/run_{run_id}", "0" * 64],
    )


def _seed_variant_with_consensus(  # noqa: PLR0913 — explicit per-variant insert keeps the test legible
    conn: DuckDBPyConnection,
    *,
    variant_id: int,
    rsid: str,
    chrom: str,
    pos: int,
    ref: str,
    alt: str,
    consensus_a1: str,
    consensus_a2: str,
    dosage: int,
    variant_type: str = "SNV",
) -> None:
    conn.execute(
        """
        INSERT INTO variants_master (
            variant_id, rsid, chrom, pos_grch38, pos_grch37, ref_allele, alt_allele,
            variant_type, has_genotyped_call, has_imputed_call, is_acmg_sf,
            liftover_chain, liftover_status
        ) VALUES (?, ?, ?::chromosome_enum, ?, ?, ?, ?, ?::variant_type_enum,
                  TRUE, FALSE, FALSE, 'native_grch38', 'native_grch38')
        """,
        [variant_id, rsid, chrom, pos, pos, ref, alt, variant_type],
    )
    conn.execute(
        """
        INSERT INTO consensus_genotypes (
            variant_id, consensus_allele_1, consensus_allele_2, is_no_call,
            dosage, consensus_method, is_imputed, contributing_calls, resolution_rule,
            confidence
        ) VALUES (?, ?, ?, FALSE, ?, 'both_concordant'::consensus_method_enum,
                  FALSE, ARRAY[]::BIGINT[], 'consensus_v1', 1.0)
        """,
        [variant_id, consensus_a1, consensus_a2, dosage],
    )
    # Genotype call needed so the input_run_ids query has something to find.
    conn.execute(
        """
        INSERT INTO genotype_calls (
            call_id, variant_id, source, source_chip_version, ingestion_run_id,
            genotype_raw, allele_1, allele_2, is_no_call,
            is_imputed, raw_strand, strand_status, quality_flags, is_active
        ) VALUES (?, ?, '23andme'::source_enum, 'test', 1,
                  ?, ?, ?, FALSE, FALSE, '+',
                  'resolved_plus'::strand_status_enum, ARRAY[]::VARCHAR[], TRUE)
        """,
        [variant_id, variant_id, consensus_a1 + consensus_a2, consensus_a1, consensus_a2],
    )


def _region_genotypes(path: Path) -> dict[int, str]:
    """Read ``{pos: genotype}`` from a region target VCF (last column is the GT)."""
    with gzip.open(path, "rt") as f:
        rows = [ln.rstrip("\n").split("\t") for ln in f if not ln.startswith("#")]
    return {int(r[1]): r[-1] for r in rows}


def test_prepare_run_writes_per_chrom_vcfs_with_beagle_headers(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture forces tmp-scoped settings
    tmp_path: Path,  # noqa: ARG001 — isolated_settings already redirects paths
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        # Three variants on chr1, two on chr2, one on X.
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs_a",
            chrom="1",
            pos=1000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=2,
            rsid="rs_b",
            chrom="1",
            pos=2000,
            ref="C",
            alt="T",
            consensus_a1="C",
            consensus_a2="C",
            dosage=0,
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=3,
            rsid="rs_c",
            chrom="1",
            pos=3000,
            ref="A",
            alt="T",
            consensus_a1="T",
            consensus_a2="T",
            dosage=2,
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=4,
            rsid="rs_d",
            chrom="2",
            pos=500,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=5,
            rsid="rs_e",
            chrom="2",
            pos=600,
            ref="C",
            alt="G",
            consensus_a1="C",
            consensus_a2="G",
            dosage=1,
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=6,
            rsid="rs_f",
            chrom="X",
            pos=42,
            ref="A",
            alt="T",
            consensus_a1="A",
            consensus_a2="T",
            dosage=1,
        )

    result = prepare_run(sample_id="testsample")

    assert result.imputation_id == 1
    assert result.variants_total == 6
    assert result.variants_per_chrom == {"1": 3, "2": 2, "X": 1}
    assert result.input_run_ids == (1,)

    # Autosomes are top-level chr*.vcf.gz; chrX is region-split under chrX_regions/
    # (M3-physical), not a top-level chrX.vcf.gz. The chrX variant (pos 42, non-PAR;
    # ambiguous sex -> diploid) lands in the non-PAR region target.
    names = {p.name for p in result.vcf_paths}
    assert "chr1.vcf.gz" in names
    assert "chr2.vcf.gz" in names
    assert "chrX.vcf.gz" not in names
    assert "nonpar.vcf.gz" in names
    assert result.archive.chrx_region_upload_path("nonpar").is_file()
    assert result.chrx_regions == {"par1": 0, "nonpar": 1, "par2": 0}
    for p in result.vcf_paths:
        assert p.is_file()
        with gzip.open(p, "rt") as f:
            content = f.read()
        assert content.startswith("##fileformat=VCFv4.2")
        assert "##contig=<ID=chr" in content
        assert "##reference=GRCh38" in content
        assert "\ttestsample\n" in content

    # Manifest is written and parseable.
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["imputation_id"] == 1
    assert manifest["reference_panel"] == "1000g_phase3_grch38"
    assert manifest["imputation_server"] == "beagle"
    assert manifest["imputation_tool"] == "beagle_5.5"
    assert manifest["build"] == "GRCh38"
    assert manifest["variants_total"] == 6
    assert manifest["chromosomes_exported"] == ["1", "2", "X"]
    # M3 chrX provenance: per-region counts + the ploidy decision (ambiguous -> diploid).
    assert manifest["chrx_regions"] == {"par1": 0, "nonpar": 1, "par2": 0}
    assert manifest["chrx_ploidy"] == "diploid"
    # Old TopMed-era manifest fields are gone.
    assert "topmed_recommended_compression" not in manifest
    assert "compression_note" not in manifest


def test_prepare_includes_chr_y_when_present(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Y is part of the Beagle-era imputable set (it was excluded under TopMed)."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs_y",
            chrom="Y",
            pos=100,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )

    result = prepare_run(sample_id="x")
    assert result.variants_per_chrom == {"Y": 1}
    assert {p.name for p in result.vcf_paths} == {"chrY.vcf.gz"}


def test_prepare_writes_one_record_per_variant_in_chrom_order(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        # Insert positions out of order to check the SQL ORDER BY clause.
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs1",
            chrom="1",
            pos=3000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=2,
            rsid="rs2",
            chrom="1",
            pos=1000,
            ref="A",
            alt="C",
            consensus_a1="A",
            consensus_a2="C",
            dosage=1,
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=3,
            rsid="rs3",
            chrom="1",
            pos=2000,
            ref="T",
            alt="G",
            consensus_a1="T",
            consensus_a2="G",
            dosage=1,
        )

    result = prepare_run(sample_id="x")
    chr1_path = next(p for p in result.vcf_paths if p.name == "chr1.vcf.gz")
    with gzip.open(chr1_path, "rt") as f:
        lines = [ln for ln in f.readlines() if not ln.startswith("#")]
    positions = [int(ln.split("\t")[1]) for ln in lines]
    assert positions == [1000, 2000, 3000]


def test_prepare_genotype_strings_reflect_dosage(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        # dosage 0, 1, 2 — should render as 0/0, 0/1, 1/1.
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs_hom_ref",
            chrom="1",
            pos=1000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="A",
            dosage=0,
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=2,
            rsid="rs_het",
            chrom="1",
            pos=2000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=3,
            rsid="rs_hom_alt",
            chrom="1",
            pos=3000,
            ref="A",
            alt="G",
            consensus_a1="G",
            consensus_a2="G",
            dosage=2,
        )

    result = prepare_run(sample_id="x")
    chr1_path = next(p for p in result.vcf_paths if p.name == "chr1.vcf.gz")
    with gzip.open(chr1_path, "rt") as f:
        rows = [ln.rstrip("\n").split("\t") for ln in f if not ln.startswith("#")]
    # Last column is the genotype.
    genotypes = [r[-1] for r in rows]
    assert genotypes == ["0/0", "0/1", "1/1"]


def test_prepare_skips_ref_equals_alt_rows(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Hom-only rows where Phase 2 set ref==alt are excluded from the upload.

    Beagle cannot impute against ``ref=A alt=A`` rows; until Phase 5 loads
    dbSNP and a future prepare can rewrite these with canonical alleles,
    they are correctly dropped at the SQL filter step.
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        # Position 1000: ref==alt (hom-only); should be skipped.
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs_homonly",
            chrom="1",
            pos=1000,
            ref="A",
            alt="A",
            consensus_a1="A",
            consensus_a2="A",
            dosage=2,
        )
        # Position 2000: ref != alt; kept.
        _seed_variant_with_consensus(
            conn,
            variant_id=2,
            rsid="rs_het",
            chrom="1",
            pos=2000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )

    result = prepare_run(sample_id="x")
    assert result.variants_total == 1
    assert result.variants_per_chrom == {"1": 1}


def test_prepare_skips_indels_and_multi_base_alleles(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs_snv",
            chrom="1",
            pos=1000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )
        # An INDEL — should be skipped by the SNV filter.
        _seed_variant_with_consensus(
            conn,
            variant_id=2,
            rsid="rs_indel",
            chrom="1",
            pos=2000,
            ref="A",
            alt="ATC",
            consensus_a1="A",
            consensus_a2="ATC",
            dosage=1,
            variant_type="INDEL",
        )

    result = prepare_run(sample_id="x")
    assert result.variants_total == 1
    assert result.variants_per_chrom == {"1": 1}


def test_prepare_raises_when_no_genotyped_ingestion_runs(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with pytest.raises(RuntimeError, match="no active 23andMe or Ancestry calls"):
        prepare_run(sample_id="x")


def test_prepare_raises_when_no_eligible_consensus(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        # A variant without a consensus row — nothing for the export to pick up.
        conn.execute(
            """
            INSERT INTO variants_master (
                variant_id, rsid, chrom, pos_grch38, pos_grch37,
                ref_allele, alt_allele, variant_type,
                has_genotyped_call, has_imputed_call, is_acmg_sf,
                liftover_chain, liftover_status
            ) VALUES (1, 'rs_x', '1'::chromosome_enum, 1, 1, 'A', 'G', 'SNV',
                      TRUE, FALSE, FALSE, 'native_grch38', 'native_grch38')
            """,
        )
        conn.execute(
            """
            INSERT INTO genotype_calls (
                call_id, variant_id, source, source_chip_version, ingestion_run_id,
                genotype_raw, allele_1, allele_2, is_no_call,
                is_imputed, raw_strand, strand_status, quality_flags, is_active
            ) VALUES (1, 1, '23andme'::source_enum, 'test', 1, 'AG', 'A', 'G',
                      FALSE, FALSE, '+',
                      'resolved_plus'::strand_status_enum, ARRAY[]::VARCHAR[], TRUE)
            """,
        )
    with pytest.raises(RuntimeError, match="no eligible SNV consensus rows"):
        prepare_run(sample_id="x")


def test_prepare_rejects_second_run_unless_force_new(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs_a",
            chrom="1",
            pos=1000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )
    prepare_run(sample_id="x")
    with pytest.raises(RuntimeError, match="already in flight"):
        prepare_run(sample_id="x")
    # force_new bypasses the gate.
    second = prepare_run(sample_id="x", force_new=True)
    assert second.imputation_id == 2


def test_pipeline_version_stamp_matches_constant(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs_a",
            chrom="1",
            pos=1000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )
    prepare_run(sample_id="x")
    with duckdb_connection() as conn:
        row = conn.execute(
            "SELECT pipeline_version FROM imputation_runs WHERE imputation_id = 1",
        ).fetchone()
    assert row[0] == EXPORT_PIPELINE_VERSION


def test_prepare_exports_recovered_chrx_and_skips_hom_only(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture redirects paths
) -> None:
    """A canonical chrX SNV (ref != alt) exports; a hom-only (ref == alt) drops.

    Post-canonicalization the recovered chrX positions carry a real ALT and flow
    through the export; the ``ref != alt`` SQL filter still drops any residual
    hom-only row. Under M3 the surviving chrX rows land in the region targets.
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        _seed_variant_with_consensus(  # recovered: ref != alt -> exported
            conn,
            variant_id=1,
            rsid="rs_recovered",
            chrom="X",
            pos=50_000_000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )
        _seed_variant_with_consensus(  # hom-only: ref == alt -> dropped
            conn,
            variant_id=2,
            rsid="rs_homonly",
            chrom="X",
            pos=50_000_001,
            ref="C",
            alt="C",
            consensus_a1="C",
            consensus_a2="C",
            dosage=0,
        )

    result = prepare_run(sample_id="x")

    assert result.variants_per_chrom == {"X": 1}
    # M3: chrX exports as region files; the non-PAR core position lands in non-PAR.
    chrx_vcf = result.archive.chrx_region_upload_path("nonpar")
    with gzip.open(chrx_vcf, "rt") as f:
        body = f.read()
    assert "\t50000000\t" in body  # recovered chrX position exported
    assert "\t50000001\t" not in body  # hom-only position dropped
    assert result.chrx_regions == {"par1": 0, "nonpar": 1, "par2": 0}


def test_prepare_chrx_male_renders_nonpar_haploid_par_diploid(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture redirects paths
) -> None:
    """M3 male export: non-PAR rows render haploid (0/1), PAR rows stay diploid."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        _seed_variant_with_consensus(  # non-PAR core, hom-ref -> haploid "0"
            conn,
            variant_id=1,
            rsid="rs1",
            chrom="X",
            pos=50_000_000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="A",
            dosage=0,
        )
        _seed_variant_with_consensus(  # non-PAR core, hom-alt -> haploid "1"
            conn,
            variant_id=2,
            rsid="rs2",
            chrom="X",
            pos=50_000_002,
            ref="A",
            alt="G",
            consensus_a1="G",
            consensus_a2="G",
            dosage=2,
        )
        _seed_variant_with_consensus(  # PAR1, het -> diploid "0/1"
            conn,
            variant_id=3,
            rsid="rs3",
            chrom="X",
            pos=1_000_000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )

    result = prepare_run(sample_id="x", sex="M")

    assert result.profile_sex == "M"
    assert result.chrx_regions == {"par1": 1, "nonpar": 2, "par2": 0}
    assert _region_genotypes(result.archive.chrx_region_upload_path("nonpar")) == {
        50_000_000: "0",
        50_000_002: "1",
    }
    assert _region_genotypes(result.archive.chrx_region_upload_path("par1")) == {
        1_000_000: "0/1",
    }
    # An empty region (no PAR2 rows) writes no file.
    assert not result.archive.chrx_region_upload_path("par2").is_file()


def test_prepare_chrx_male_nonpar_dosage1_is_haploid_no_call(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture redirects paths
) -> None:
    """A male non-PAR het (dosage 1, biologically impossible) renders as ``.`` for Beagle."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs1",
            chrom="X",
            pos=50_000_000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )

    result = prepare_run(sample_id="x", sex="M")

    assert _region_genotypes(result.archive.chrx_region_upload_path("nonpar")) == {
        50_000_000: ".",
    }


def test_prepare_chrx_female_renders_nonpar_diploid(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture redirects paths
) -> None:
    """M3 female export: chrX non-PAR renders diploid (a female het stays ``0/1``)."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs1",
            chrom="X",
            pos=50_000_000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )

    result = prepare_run(sample_id="x", sex="F")

    assert result.profile_sex == "F"
    assert _region_genotypes(result.archive.chrx_region_upload_path("nonpar")) == {
        50_000_000: "0/1",
    }
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["chrx_ploidy"] == "diploid"


def test_prepare_chrx_buckets_across_three_regions(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture redirects paths
) -> None:
    """chrX rows split into PAR1 / non-PAR / PAR2; both slivers count as non-PAR."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        rows = [
            (1, "rs_sliver", 5_000, 0),  # non-PAR lower sliver
            (2, "rs_par1", 1_000_000, 1),  # PAR1
            (3, "rs_core", 50_000_000, 2),  # non-PAR core
            (4, "rs_par2", 155_800_000, 1),  # PAR2
        ]
        for variant_id, rsid, pos, dosage in rows:
            a1, a2 = ("A", "G") if dosage == 1 else (("A", "A") if dosage == 0 else ("G", "G"))
            _seed_variant_with_consensus(
                conn,
                variant_id=variant_id,
                rsid=rsid,
                chrom="X",
                pos=pos,
                ref="A",
                alt="G",
                consensus_a1=a1,
                consensus_a2=a2,
                dosage=dosage,
            )

    result = prepare_run(sample_id="x", sex="M")

    assert result.variants_per_chrom["X"] == 4  # whole-chromosome total
    assert result.chrx_regions == {"par1": 1, "nonpar": 2, "par2": 1}
    assert set(_region_genotypes(result.archive.chrx_region_upload_path("par1"))) == {1_000_000}
    assert set(_region_genotypes(result.archive.chrx_region_upload_path("par2"))) == {155_800_000}
    assert set(_region_genotypes(result.archive.chrx_region_upload_path("nonpar"))) == {
        5_000,
        50_000_000,
    }
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["chrx_regions"] == {"par1": 1, "nonpar": 2, "par2": 1}
    assert manifest["chrx_ploidy"] == "male_nonpar_haploid"


def test_prepare_manifest_records_profile_sex(
    isolated_settings: dict[str, str],  # noqa: ARG001 — fixture redirects paths
) -> None:
    """The prepare manifest carries the resolved profile sex (transient provenance)."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_ingestion_run(conn, 1, "23andme")
        conn.execute(
            "INSERT INTO sample_qc (qc_id, run_id, sex_inferred, qc_status) "
            "VALUES (1, 1, 'M', 'pass')",
        )
        _seed_variant_with_consensus(
            conn,
            variant_id=1,
            rsid="rs_a",
            chrom="1",
            pos=1000,
            ref="A",
            alt="G",
            consensus_a1="A",
            consensus_a2="G",
            dosage=1,
        )

    # auto resolves to the chip aggregate ('M')...
    result = prepare_run(sample_id="x")
    assert result.profile_sex == "M"
    assert json.loads(result.manifest_path.read_text())["profile_sex"] == "M"

    # ...and an explicit --sex override wins.
    forced = prepare_run(sample_id="x", force_new=True, sex="F")
    assert forced.profile_sex == "F"
    assert json.loads(forced.manifest_path.read_text())["profile_sex"] == "F"
