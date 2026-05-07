"""Schema initialization end-to-end."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from genome.db import duckdb_connection, init_databases, sqlcipher_connection
from genome.db.init_schema import _apply_duckdb_ddl

_EXPECTED_DUCKDB_TABLES = {
    # group 1
    "variants_master",
    "genotype_calls",
    "consensus_genotypes",
    "discrepancies",
    "ingestion_runs",
    "imputation_runs",
    "sample_qc",
    # group 2
    "annotation_source_versions",
    "dbsnp_annotations",
    "variant_aliases",
    "clinvar_annotations",
    "gwas_catalog_associations",
    "gnomad_frequencies",
    "vep_consequences",
    "pharmgkb_annotations",
    "cpic_guidelines",
    "pgs_catalog_scores",
    "pgs_score_weights",
    "genes",
    "traits",
    "pathways",
    "pathway_genes",
    "variant_annotations_index",
    # group 3
    "analysis_runs",
    "derived_pgs",
    "derived_pgx_phenotypes",
    "derived_carrier_findings",
    "derived_acmg_sf_findings",
    "derived_hla_typing",
    "derived_roh",
    "derived_haplogroups",
    "derived_global_ancestry",
    "derived_local_ancestry",
    "derived_archaic_ancestry",
    "derived_genetic_distance",
    "derived_compound_het",
    "derived_genome_qc",
    # group 4
    "insights",
    "evidence",
    "insight_variants",
    "insight_genes",
    "insight_traits",
    "summary_dashboard",
}

_EXPECTED_DUCKDB_VIEWS = {
    # group 1
    "concordance_summary_v",
    "platform_coverage_v",
    "call_comparison_v",
    # group 2
    "variant_full_v",
    "gene_variant_summary_v",
    "user_pgx_variants_v",
    # group 3
    "pgx_phenotype_drugs_v",
    "acmg_sf_active_v",
    "pgs_extremes_v",
    "carrier_panel_v",
    "derived_summary_v",
    # group 4
    "gene_rollup_v",
    "pleiotropy_v",
    "compound_effects_v",
}

# Indexes that used to be skipped because they were partial (DDL had `WHERE`).
# Once the schema is DuckDB-clean they should land normally.
_EXPECTED_DUCKDB_INDEXES = {
    "idx_vm_acmg_sf",
    "idx_disc_unresolved",
}

_EXPECTED_SQLITE_TABLES = {
    "profiles",
    "notes",
    "bookmarks",
    "observation_phenotypes",
    "observations",
    "medications",
    "saved_queries",
    "query_history",
    "audit_log",
    "snapshots",
    "jobs",
    "user_preferences",
}


def _duckdb_tables(path: Path) -> set[str]:
    with duckdb_connection(path, read_only=True) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'main' AND table_type = 'BASE TABLE'",
        ).fetchall()
    return {r[0] for r in rows}


def _duckdb_views(path: Path) -> set[str]:
    with duckdb_connection(path, read_only=True) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'main' AND table_type = 'VIEW'",
        ).fetchall()
    return {r[0] for r in rows}


def _duckdb_indexes(path: Path) -> set[str]:
    with duckdb_connection(path, read_only=True) as conn:
        rows = conn.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
    return {r[0] for r in rows}


def _sqlite_tables(path: Path) -> set[str]:
    with sqlcipher_connection(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
        ).fetchall()
    return {r[0] for r in rows}


def test_init_creates_both_databases(isolated_settings: dict[str, str]) -> None:
    duckdb_path = Path(isolated_settings["GENOME_DUCKDB_PATH"])
    sqlite_path = Path(isolated_settings["APP_DB_PATH"])

    assert not duckdb_path.exists()
    assert not sqlite_path.exists()

    result = init_databases()

    assert result.duckdb_created is True
    assert result.sqlite_created is True
    assert duckdb_path.exists()
    assert sqlite_path.exists()


def test_expected_duckdb_tables_present(isolated_settings: dict[str, str]) -> None:
    init_databases()
    tables = _duckdb_tables(Path(isolated_settings["GENOME_DUCKDB_PATH"]))
    missing = _EXPECTED_DUCKDB_TABLES - tables
    assert not missing, f"missing DuckDB tables: {sorted(missing)}"


def test_expected_duckdb_views_present(isolated_settings: dict[str, str]) -> None:
    init_databases()
    views = _duckdb_views(Path(isolated_settings["GENOME_DUCKDB_PATH"]))
    missing = _EXPECTED_DUCKDB_VIEWS - views
    assert not missing, f"missing DuckDB views: {sorted(missing)}"


def test_previously_partial_indexes_present(isolated_settings: dict[str, str]) -> None:
    init_databases()
    indexes = _duckdb_indexes(Path(isolated_settings["GENOME_DUCKDB_PATH"]))
    missing = _EXPECTED_DUCKDB_INDEXES - indexes
    assert not missing, f"missing DuckDB indexes: {sorted(missing)}"


def test_apply_duckdb_ddl_raises_on_failure(tmp_path: Path) -> None:
    """A bad DDL statement must propagate (no skip-on-fail anymore)."""
    bad = tmp_path / "bad.sql"
    bad.write_text("CREATE TABLE not_a_real_thing AS SELECT * FROM nope;")
    with duckdb.connect(":memory:") as conn, pytest.raises(duckdb.Error):
        _apply_duckdb_ddl(conn, [bad])


def test_expected_sqlite_tables_present(isolated_settings: dict[str, str]) -> None:
    init_databases()
    tables = _sqlite_tables(Path(isolated_settings["APP_DB_PATH"]))
    missing = _EXPECTED_SQLITE_TABLES - tables
    assert not missing, f"missing SQLite tables: {sorted(missing)}"


def test_seed_profile_present(isolated_settings: dict[str, str]) -> None:
    init_databases()
    with sqlcipher_connection(Path(isolated_settings["APP_DB_PATH"])) as conn:
        rows = conn.execute(
            "SELECT profile_id, name, relationship FROM profiles ORDER BY profile_id"
        ).fetchall()
    assert rows == [(1, "Me", "self")]


def test_seed_user_preferences_present(isolated_settings: dict[str, str]) -> None:
    init_databases()
    with sqlcipher_connection(Path(isolated_settings["APP_DB_PATH"])) as conn:
        rows = dict(conn.execute("SELECT pref_key, pref_value FROM user_preferences").fetchall())
    expected = {
        "current_profile_id",
        "default_audience",
        "imputation_r2_threshold",
        "theme",
        "llm_model",
        "audit_retention_days",
        "external_calls_enabled",
        "pubmed_enrichment_enabled",
        "auto_snapshot_cadence",
        "prs_min_coverage_pct",
        "font_size",
        "cite_in_responses",
    }
    assert expected <= set(rows)
    assert rows["llm_model"] == "claude-opus-4-7"
    assert rows["default_audience"] == "layperson"


def test_init_is_idempotent(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    first = init_databases()
    second = init_databases()
    assert first.duckdb_created is True
    assert first.sqlite_created is True
    assert second.duckdb_created is False
    assert second.sqlite_created is False


def test_duckdb_file_perms_owner_only(isolated_settings: dict[str, str]) -> None:
    init_databases()
    path = Path(isolated_settings["GENOME_DUCKDB_PATH"])
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, f"DuckDB file perms should be 0600, got {oct(mode)}"
