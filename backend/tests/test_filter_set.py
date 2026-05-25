"""Tests for :mod:`genome.annotate.filter_set`.

Covers both filter strategies of ``build_filter_set``:

* ``three_way`` — gnomAD's ``(user U ClinVar U GWAS)`` union, joined through the
  ``annotation_sources`` version pointer; composition
  ``{user, clinvar, gwas, union_total}``.
* ``user_only`` — dbSNP's filter; ``variants_master`` positions alone (ClinVar /
  GWAS rows present in the DB must NOT contribute); composition
  ``{user, union_total}``.

Plus the chrom allow-list (Y/MT included only when ``supported_chroms`` has
them) and the ``pos_grch38 > 0`` sentinel guard (finding-013 #11).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from genome.annotate.filter_set import build_filter_set
from genome.annotate.source_versions import insert_source_version
from genome.annotate.supersession import flip_to_new_version
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

_GNOMAD_CHROMS = (*(str(n) for n in range(1, 23)), "X")
_DBSNP_CHROMS = (*(str(n) for n in range(1, 23)), "X", "Y", "MT")


@pytest.fixture(autouse=True)
def _isolated(isolated_settings: dict[str, str]) -> None:
    """Point every test at the tmp-dir databases."""


def _seed_user_variants(conn: DuckDBPyConnection, rows: list[tuple[str, int]]) -> None:
    for chrom, pos in rows:
        conn.execute(
            """
            INSERT INTO variants_master (chrom, pos_grch38, ref_allele, alt_allele)
            VALUES (?::chromosome_enum, ?, 'A', 'C')
            """,
            [chrom, pos],
        )


def _seed_clinvar_active(conn: DuckDBPyConnection, rows: list[tuple[str, int]]) -> int:
    sv_id = insert_source_version(
        conn,
        source_db="clinvar",
        version="2026_05_10",
        source_url=None,
        source_file_hash="c" * 64,
        source_file_size=1,
        record_count=len(rows),
    )
    base_row = conn.execute(
        "SELECT COALESCE(MAX(clinvar_id), 0) FROM clinvar_annotations"
    ).fetchone()
    base = (int(base_row[0]) if base_row is not None else 0) + 1
    for i, (chrom, pos) in enumerate(rows):
        conn.execute(
            """
            INSERT INTO clinvar_annotations (
                clinvar_id, variation_id, chrom, pos_grch38, source_version_id, retrieval_date
            )
            VALUES (?, ?, ?::chromosome_enum, ?, ?, CURRENT_TIMESTAMP)
            """,
            [base + i, f"CV{base + i}", chrom, pos, sv_id],
        )
    flip_to_new_version(
        conn,
        source="clinvar",
        table="clinvar_annotations",
        new_source_version_id=sv_id,
    )
    return sv_id


def _seed_gwas_active(conn: DuckDBPyConnection, rows: list[tuple[str, int]]) -> int:
    sv_id = insert_source_version(
        conn,
        source_db="gwas_catalog",
        version="2026_05_16",
        source_url=None,
        source_file_hash="g" * 64,
        source_file_size=1,
        record_count=len(rows),
    )
    base_row = conn.execute(
        "SELECT COALESCE(MAX(association_id), 0) FROM gwas_catalog_associations",
    ).fetchone()
    base = (int(base_row[0]) if base_row is not None else 0) + 1
    for i, (chrom, pos) in enumerate(rows):
        conn.execute(
            """
            INSERT INTO gwas_catalog_associations (
                association_id, study_accession, rsid, chrom, pos_grch38,
                source_version_id, retrieval_date
            )
            VALUES (?, ?, ?, ?::chromosome_enum, ?, ?, CURRENT_TIMESTAMP)
            """,
            [base + i, f"GCST{base + i}", f"rs{1000 + base + i}", chrom, pos, sv_id],
        )
    flip_to_new_version(
        conn,
        source="gwas_catalog",
        table="gwas_catalog_associations",
        new_source_version_id=sv_id,
    )
    return sv_id


def test_three_way_union_and_composition() -> None:
    """three_way unions user + active ClinVar + active GWAS positions."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("1", 100), ("1", 200), ("2", 300)])
        _seed_clinvar_active(conn, [("1", 200), ("1", 400)])
        _seed_gwas_active(conn, [("2", 300), ("X", 500)])
        result = build_filter_set(conn, strategy="three_way", supported_chroms=_GNOMAD_CHROMS)
    assert result.positions["1"] == [100, 200, 400]
    assert result.positions["2"] == [300]
    assert result.positions["X"] == [500]
    assert result.composition == {"user": 3, "clinvar": 2, "gwas": 2, "union_total": 5}


def test_user_only_ignores_clinvar_and_gwas() -> None:
    """user_only takes ONLY variants_master positions; ClinVar/GWAS don't contribute."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("1", 100), ("22", 999)])
        # These are active but must NOT widen the user_only filter set.
        _seed_clinvar_active(conn, [("1", 555), ("3", 777)])
        _seed_gwas_active(conn, [("4", 888)])
        result = build_filter_set(conn, strategy="user_only", supported_chroms=_DBSNP_CHROMS)
    assert result.positions["1"] == [100]
    assert result.positions["22"] == [999]
    # No ClinVar/GWAS positions leaked in.
    assert result.positions["3"] == []
    assert result.positions["4"] == []
    # Composition has only user + union_total (no clinvar/gwas keys).
    assert result.composition == {"user": 2, "union_total": 2}


def test_user_only_includes_y_and_mt_when_supported() -> None:
    """dbSNP's allow-list keeps Y + MT user positions (gnomAD's would drop them)."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("Y", 2800017), ("MT", 73), ("1", 100)])
        dbsnp_result = build_filter_set(conn, strategy="user_only", supported_chroms=_DBSNP_CHROMS)
        gnomad_result = build_filter_set(
            conn,
            strategy="user_only",
            supported_chroms=_GNOMAD_CHROMS,
        )
    assert dbsnp_result.positions["Y"] == [2800017]
    assert dbsnp_result.positions["MT"] == [73]
    assert dbsnp_result.composition["union_total"] == 3
    # The gnomAD-style allow-list has no Y/MT keys and drops those positions.
    assert "Y" not in gnomad_result.positions
    assert "MT" not in gnomad_result.positions
    assert gnomad_result.composition["union_total"] == 1


def test_sentinel_negative_positions_excluded_both_strategies() -> None:
    """``pos_grch38 = -1`` sentinels are dropped from every subquery + the union."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_user_variants(conn, [("22", 17007792), ("1", 500)])
        _seed_clinvar_active(conn, [("22", 17007792), ("22", -1), ("1", -1)])
        _seed_gwas_active(conn, [("22", 17007800), ("22", -1)])
        three_way = build_filter_set(conn, strategy="three_way", supported_chroms=_GNOMAD_CHROMS)
        user_only = build_filter_set(conn, strategy="user_only", supported_chroms=_DBSNP_CHROMS)
    for chrom, positions in three_way.positions.items():
        assert all(p > 0 for p in positions), f"chrom {chrom} kept a non-positive position"
    assert -1 not in three_way.positions["22"]
    assert 17007792 in three_way.positions["22"]
    assert 17007800 in three_way.positions["22"]
    # user_only never sees the ClinVar/GWAS sentinels at all (only user rows).
    assert user_only.positions["22"] == [17007792]
    assert user_only.composition == {"user": 2, "union_total": 2}
