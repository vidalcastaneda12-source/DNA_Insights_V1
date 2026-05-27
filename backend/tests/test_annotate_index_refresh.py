"""Tests for :mod:`genome.annotate.index_refresh`.

Exercises the ``variant_annotations_index`` rollup builder against the real
schema (created by ``init_databases``). The shape mirrors
``test_annotate_supersession``: seed ``variants_master`` + the per-source
annotation tables under a ``source_version_id``, flip the
``annotation_sources`` pointer via :func:`flip_to_new_version`, then call
:func:`refresh_index` on the open connection and assert on the materialized
rows.

The four contributing sources join differently — ClinVar / gnomAD on full
GRCh38 coords, GWAS Catalog / PharmGKB on rsid — so the helpers below seed each
in its own grain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import structlog

from genome.annotate.index_refresh import IndexRefreshResult, refresh_index
from genome.annotate.source_versions import insert_source_version
from genome.annotate.supersession import flip_to_new_version
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from duckdb import DuckDBPyConnection


@pytest.fixture(autouse=True)
def _reset_structlog_after_each_test() -> Iterator[None]:
    """Restore structlog defaults so capture_logs doesn't leak between tests."""
    try:
        yield
    finally:
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Seeding helpers.
# ---------------------------------------------------------------------------


def _new_version(conn: DuckDBPyConnection, *, source_db: str, version: str, hash_char: str) -> int:
    """Allocate a fresh ``annotation_source_versions`` row, return its id."""
    return insert_source_version(
        conn,
        source_db=source_db,
        version=version,
        source_url=None,
        source_file_hash=hash_char * 64,
        source_file_size=1,
        record_count=0,
    )


def _activate(conn: DuckDBPyConnection, *, source_db: str, table: str, sv_id: int) -> None:
    """Flip the ``annotation_sources`` pointer for ``source_db`` to ``sv_id``."""
    flip_to_new_version(conn, source=source_db, table=table, new_source_version_id=sv_id)


def _seed_variant(  # noqa: PLR0913 — variant identity fields are not collapsible
    conn: DuckDBPyConnection,
    variant_id: int,
    *,
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alt: str = "G",
    rsid: str | None = "rs1",
) -> None:
    """Insert one ``variants_master`` row with an explicit ``variant_id``."""
    conn.execute(
        """
        INSERT INTO variants_master
            (variant_id, rsid, chrom, pos_grch38, ref_allele, alt_allele)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [variant_id, rsid, chrom, pos, ref, alt],
    )


def _seed_clinvar(  # noqa: PLR0913 — annotation fields are not collapsible
    conn: DuckDBPyConnection,
    sv_id: int,
    *,
    clinvar_id: int,
    significance: str | None,
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alt: str = "G",
    star_rating: int = 1,
    conditions: Sequence[str] | None = None,
) -> None:
    """Insert one ``clinvar_annotations`` row (coord-keyed join)."""
    conn.execute(
        """
        INSERT INTO clinvar_annotations
            (clinvar_id, chrom, pos_grch38, ref_allele, alt_allele,
             clinical_significance, star_rating, conditions,
             source_version_id, retrieval_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [
            clinvar_id,
            chrom,
            pos,
            ref,
            alt,
            significance,
            star_rating,
            list(conditions) if conditions is not None else None,
            sv_id,
        ],
    )


def _seed_gwas(  # noqa: PLR0913 — annotation fields are not collapsible
    conn: DuckDBPyConnection,
    sv_id: int,
    *,
    association_id: int,
    rsid: str,
    trait_name: str | None,
    p_value: float | None,
) -> None:
    """Insert one ``gwas_catalog_associations`` row (rsid-keyed join)."""
    conn.execute(
        """
        INSERT INTO gwas_catalog_associations
            (association_id, rsid, trait_name, p_value, source_version_id, retrieval_date)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [association_id, rsid, trait_name, p_value, sv_id],
    )


def _seed_gnomad(  # noqa: PLR0913 — annotation fields are not collapsible
    conn: DuckDBPyConnection,
    sv_id: int,
    *,
    freq_id: int,
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alt: str = "G",
    af_global: float | None,
    pops: dict[str, float] | None = None,
) -> None:
    """Insert one ``gnomad_frequencies`` row (coord-keyed join).

    ``pops`` optionally sets a subset of the 10 per-population AF columns;
    unset populations stay NULL.
    """
    pops = pops or {}
    conn.execute(
        """
        INSERT INTO gnomad_frequencies
            (freq_id, chrom, pos_grch38, ref_allele, alt_allele, af_global,
             af_afr, af_ami, af_amr, af_asj, af_eas,
             af_fin, af_mid, af_nfe, af_sas, af_oth,
             source_version_id, retrieval_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [
            freq_id,
            chrom,
            pos,
            ref,
            alt,
            af_global,
            pops.get("afr"),
            pops.get("ami"),
            pops.get("amr"),
            pops.get("asj"),
            pops.get("eas"),
            pops.get("fin"),
            pops.get("mid"),
            pops.get("nfe"),
            pops.get("sas"),
            pops.get("oth"),
            sv_id,
        ],
    )


def _seed_pharmgkb(
    conn: DuckDBPyConnection,
    sv_id: int,
    *,
    pharmgkb_id: int,
    rsid: str,
    drug_name: str | None,
) -> None:
    """Insert one ``pharmgkb_annotations`` row (rsid-keyed join)."""
    conn.execute(
        """
        INSERT INTO pharmgkb_annotations
            (pharmgkb_id, rsid, drug_name, source_version_id, retrieval_date)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [pharmgkb_id, rsid, drug_name, sv_id],
    )


def _seed_cpic(
    conn: DuckDBPyConnection,
    sv_id: int,
    *,
    guideline_id: int,
    gene_symbol: str,
    drug_name: str,
) -> None:
    """Insert one ``cpic_guidelines`` row (gene+drug grain; no variant linkage)."""
    conn.execute(
        """
        INSERT INTO cpic_guidelines
            (guideline_id, gene_symbol, drug_name, source_version_id, retrieval_date)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [guideline_id, gene_symbol, drug_name, sv_id],
    )


def _fetch_index_rows(conn: DuckDBPyConnection) -> list[dict[str, Any]]:
    """Return every ``variant_annotations_index`` row as a name→value dict.

    Uses ``SELECT *`` + the cursor description so the mapping stays robust to
    the DDL column order without an f-string SQL (S608).
    """
    cur = conn.execute("SELECT * FROM variant_annotations_index ORDER BY variant_id")
    col_names = [d[0] for d in cur.description]
    return [dict(zip(col_names, row, strict=True)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Single-source coverage.
# ---------------------------------------------------------------------------


def test_clinvar_only_variant(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """ClinVar-only variant: clinvar columns populated, others at absent-values."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        sv = _new_version(conn, source_db="clinvar", version="2026_05_10", hash_char="a")
        _seed_clinvar(
            conn,
            sv,
            clinvar_id=1,
            significance="Pathogenic",
            star_rating=3,
            conditions=["Cardiomyopathy"],
        )
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=sv)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["variant_id"] == 1
    assert row["clinvar_significance"] == "Pathogenic"
    assert row["clinvar_star_rating"] == 3
    assert row["clinvar_count"] == 1
    assert row["clinvar_conditions"] == ["Cardiomyopathy"]
    # ClinVar contributed; the other three did not.
    assert row["gwas_trait_count"] == 0
    assert row["gwas_traits"] == []
    assert row["gwas_min_p_value"] is None
    assert row["af_global"] is None
    assert row["is_rare"] is None  # no gnomAD AF → rarity unknown
    assert row["is_ultrarare"] is None
    assert row["has_pgx"] is False
    assert row["pgx_drug_count"] == 0
    assert row["pgx_drugs"] == []
    assert row["is_curated"] is True  # ClinVar is a curated source


def test_gwas_only_variant(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """GWAS-only variant: gwas columns populated; is_curated FALSE (not curated)."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, rsid="rs99")
        sv = _new_version(conn, source_db="gwas_catalog", version="v1.0", hash_char="b")
        _seed_gwas(conn, sv, association_id=1, rsid="rs99", trait_name="Height", p_value=1e-8)
        _activate(conn, source_db="gwas_catalog", table="gwas_catalog_associations", sv_id=sv)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["gwas_trait_count"] == 1
    assert row["gwas_traits"] == ["Height"]
    assert row["gwas_strongest_trait"] == "Height"
    assert row["gwas_min_p_value"] == 1e-8
    assert row["clinvar_count"] == 0
    assert row["clinvar_significance"] is None
    assert row["has_pgx"] is False
    assert row["is_curated"] is False  # GWAS is not a "curated" source


def test_gnomad_only_variant(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """gnomAD-only variant: AF columns populated; counts 0, arrays empty."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="2", pos=5000, ref="C", alt="T", rsid="rs7")
        sv = _new_version(conn, source_db="gnomad", version="4.1.1", hash_char="c")
        _seed_gnomad(
            conn,
            sv,
            freq_id=1,
            chrom="2",
            pos=5000,
            ref="C",
            alt="T",
            af_global=0.25,
            pops={"afr": 0.1, "nfe": 0.4},
        )
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=sv)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["af_global"] == 0.25
    assert row["af_max_population"] == 0.4
    assert row["af_min_population"] == 0.1
    assert row["is_rare"] is False
    assert row["is_ultrarare"] is False
    assert row["clinvar_count"] == 0
    assert row["clinvar_conditions"] == []
    assert row["gwas_trait_count"] == 0
    assert row["has_pgx"] is False
    assert row["is_curated"] is False


def test_pharmgkb_only_variant(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """PharmGKB-only variant: has_pgx TRUE, drugs populated, is_curated TRUE."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, rsid="rs1801133")
        sv = _new_version(conn, source_db="pharmgkb", version="2026_05", hash_char="d")
        _seed_pharmgkb(conn, sv, pharmgkb_id=1, rsid="rs1801133", drug_name="warfarin")
        _activate(conn, source_db="pharmgkb", table="pharmgkb_annotations", sv_id=sv)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["has_pgx"] is True
    assert row["pgx_drug_count"] == 1
    assert row["pgx_drugs"] == ["warfarin"]
    assert row["is_curated"] is True  # PharmGKB is a curated source
    assert row["clinvar_count"] == 0
    assert row["af_global"] is None


# ---------------------------------------------------------------------------
# Cross-key merge — the highest-risk path now that the COALESCE chain is gone.
# ---------------------------------------------------------------------------


def test_clinvar_coord_plus_gwas_rsid_merge_to_one_row(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """One variant matched by ClinVar (coords) AND GWAS (rsid) → single merged row."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        cv = _new_version(conn, source_db="clinvar", version="cv1", hash_char="a")
        _seed_clinvar(conn, cv, clinvar_id=1, significance="Benign", conditions=["Trait A"])
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        gw = _new_version(conn, source_db="gwas_catalog", version="gw1", hash_char="b")
        _seed_gwas(conn, gw, association_id=1, rsid="rs1", trait_name="Height", p_value=1e-9)
        _activate(conn, source_db="gwas_catalog", table="gwas_catalog_associations", sv_id=gw)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1  # no double-count
    row = rows[0]
    assert row["clinvar_significance"] == "Benign"
    assert row["clinvar_count"] == 1
    assert row["gwas_trait_count"] == 1
    assert row["gwas_strongest_trait"] == "Height"
    assert row["is_curated"] is True


def test_gnomad_plus_pharmgkb_only_merge(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A variant in only the later CTEs (gnomAD + PharmGKB) still merges to one row."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="3", pos=2000, ref="G", alt="A", rsid="rs55")
        gn = _new_version(conn, source_db="gnomad", version="gn1", hash_char="c")
        _seed_gnomad(conn, gn, freq_id=1, chrom="3", pos=2000, ref="G", alt="A", af_global=0.02)
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=gn)
        pg = _new_version(conn, source_db="pharmgkb", version="pg1", hash_char="d")
        _seed_pharmgkb(conn, pg, pharmgkb_id=1, rsid="rs55", drug_name="codeine")
        _activate(conn, source_db="pharmgkb", table="pharmgkb_annotations", sv_id=pg)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["af_global"] == 0.02
    assert row["is_rare"] is False  # 0.02 >= 0.01
    assert row["has_pgx"] is True
    assert row["pgx_drugs"] == ["codeine"]
    assert row["clinvar_count"] == 0
    assert row["is_curated"] is True  # via PharmGKB


# ---------------------------------------------------------------------------
# Reductions — worst significance, strongest trait, deduped+sorted arrays.
# ---------------------------------------------------------------------------


def test_clinvar_worst_significance_and_count(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Two ClinVar rows (Benign + Pathogenic) → worst='Pathogenic', count=2."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        cv = _new_version(conn, source_db="clinvar", version="cv1", hash_char="a")
        _seed_clinvar(
            conn, cv, clinvar_id=1, significance="Benign", star_rating=1, conditions=["Cond B"]
        )
        _seed_clinvar(
            conn, cv, clinvar_id=2, significance="Pathogenic", star_rating=4, conditions=["Cond A"]
        )
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["clinvar_significance"] == "Pathogenic"  # worst by severity rank
    assert row["clinvar_star_rating"] == 4  # highest star
    assert row["clinvar_count"] == 2
    assert row["clinvar_conditions"] == ["Cond A", "Cond B"]  # deduped + byte-sorted


def test_gwas_strongest_trait_is_min_p(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Two GWAS traits at different p → strongest = min-p trait; min_p captured."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, rsid="rs1")
        gw = _new_version(conn, source_db="gwas_catalog", version="gw1", hash_char="b")
        _seed_gwas(conn, gw, association_id=1, rsid="rs1", trait_name="Weak", p_value=1e-3)
        _seed_gwas(conn, gw, association_id=2, rsid="rs1", trait_name="Strong", p_value=5e-12)
        _activate(conn, source_db="gwas_catalog", table="gwas_catalog_associations", sv_id=gw)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    row = rows[0]
    assert row["gwas_trait_count"] == 2
    assert row["gwas_min_p_value"] == 5e-12
    assert row["gwas_strongest_trait"] == "Strong"
    assert row["gwas_traits"] == ["Strong", "Weak"]  # deduped + sorted


def test_pharmgkb_drugs_deduped_and_sorted(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Repeated + out-of-order drugs collapse to a sorted distinct list."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, rsid="rs1")
        pg = _new_version(conn, source_db="pharmgkb", version="pg1", hash_char="d")
        _seed_pharmgkb(conn, pg, pharmgkb_id=1, rsid="rs1", drug_name="warfarin")
        _seed_pharmgkb(conn, pg, pharmgkb_id=2, rsid="rs1", drug_name="aspirin")
        _seed_pharmgkb(conn, pg, pharmgkb_id=3, rsid="rs1", drug_name="warfarin")
        _activate(conn, source_db="pharmgkb", table="pharmgkb_annotations", sv_id=pg)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    row = rows[0]
    assert row["pgx_drug_count"] == 2  # distinct
    assert row["pgx_drugs"] == ["aspirin", "warfarin"]


def test_gnomad_af_max_min_over_populations(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """af_max/af_min span the populated pops; unset pops are skipped, not zero."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G")
        gn = _new_version(conn, source_db="gnomad", version="gn1", hash_char="c")
        _seed_gnomad(
            conn,
            gn,
            freq_id=1,
            af_global=0.3,
            pops={"afr": 0.05, "eas": 0.5, "nfe": 0.2},
        )
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=gn)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    row = rows[0]
    assert row["af_max_population"] == 0.5
    assert row["af_min_population"] == 0.05  # NULL pops skipped, not treated as 0


# ---------------------------------------------------------------------------
# Rarity — SQL 3-valued logic.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("af_global", "expect_rare", "expect_ultrarare"),
    [
        (0.005, True, False),  # < 0.01 but >= 0.001
        (0.0005, True, True),  # < 0.001
        (0.5, False, False),  # common
    ],
)
def test_rarity_flags_from_af(
    af_global: float,
    expect_rare: bool,  # noqa: FBT001
    expect_ultrarare: bool,  # noqa: FBT001
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """is_rare / is_ultrarare follow af_global thresholds."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G")
        gn = _new_version(conn, source_db="gnomad", version="gn1", hash_char="c")
        _seed_gnomad(conn, gn, freq_id=1, af_global=af_global)
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=gn)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    row = rows[0]
    assert row["is_rare"] is expect_rare
    assert row["is_ultrarare"] is expect_ultrarare


def test_rarity_null_when_no_gnomad(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """No gnomAD AF → is_rare / is_ultrarare are NULL (rarity genuinely unknown)."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, rsid="rs1")
        cv = _new_version(conn, source_db="clinvar", version="cv1", hash_char="a")
        _seed_clinvar(conn, cv, clinvar_id=1, significance="Pathogenic")
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    row = rows[0]
    assert row["af_global"] is None
    assert row["is_rare"] is None
    assert row["is_ultrarare"] is None


# ---------------------------------------------------------------------------
# Absent-source value contract + within-CTE edge cases.
# ---------------------------------------------------------------------------


def test_clinvar_match_with_all_null_conditions_yields_empty_array(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A ClinVar match whose rows all have NULL conditions → conditions = []."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G")
        cv = _new_version(conn, source_db="clinvar", version="cv1", hash_char="a")
        _seed_clinvar(conn, cv, clinvar_id=1, significance="Pathogenic", conditions=None)
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    row = rows[0]
    assert row["clinvar_count"] == 1
    assert row["clinvar_conditions"] == []  # NULL agg → COALESCE to []


def test_counts_arrays_and_flags_never_null_on_any_row(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Counts / arrays / has_pgx / is_curated are never NULL, whichever source matched."""
    init_databases()
    with duckdb_connection() as conn:
        # One variant per source so every absent-source branch is exercised.
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rsCV")
        _seed_variant(conn, 2, chrom="1", pos=2000, ref="A", alt="G", rsid="rsGW")
        _seed_variant(conn, 3, chrom="1", pos=3000, ref="A", alt="G", rsid="rsGN")
        _seed_variant(conn, 4, chrom="1", pos=4000, ref="A", alt="G", rsid="rsPG")
        cv = _new_version(conn, source_db="clinvar", version="cv1", hash_char="a")
        _seed_clinvar(conn, cv, clinvar_id=1, significance="Benign", chrom="1", pos=1000)
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        gw = _new_version(conn, source_db="gwas_catalog", version="gw1", hash_char="b")
        _seed_gwas(conn, gw, association_id=1, rsid="rsGW", trait_name="T", p_value=1e-5)
        _activate(conn, source_db="gwas_catalog", table="gwas_catalog_associations", sv_id=gw)
        gn = _new_version(conn, source_db="gnomad", version="gn1", hash_char="c")
        _seed_gnomad(conn, gn, freq_id=1, chrom="1", pos=3000, af_global=0.2)
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=gn)
        pg = _new_version(conn, source_db="pharmgkb", version="pg1", hash_char="d")
        _seed_pharmgkb(conn, pg, pharmgkb_id=1, rsid="rsPG", drug_name="drugX")
        _activate(conn, source_db="pharmgkb", table="pharmgkb_annotations", sv_id=pg)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 4
    for row in rows:
        assert row["clinvar_count"] is not None
        assert row["gwas_trait_count"] is not None
        assert row["pgx_drug_count"] is not None
        assert row["clinvar_conditions"] is not None
        assert row["gwas_traits"] is not None
        assert row["pgx_drugs"] is not None
        assert row["has_pgx"] is not None
        assert row["is_curated"] is not None


# ---------------------------------------------------------------------------
# NULL-ship invariants — VEP + is_acmg_sf are Phase-6 placeholders.
# ---------------------------------------------------------------------------


def test_vep_and_acmg_columns_ship_null(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """All four VEP columns and is_acmg_sf are NULL on every row (Phase 6 fills them)."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        cv = _new_version(conn, source_db="clinvar", version="cv1", hash_char="a")
        _seed_clinvar(conn, cv, clinvar_id=1, significance="Pathogenic")
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        gn = _new_version(conn, source_db="gnomad", version="gn1", hash_char="c")
        _seed_gnomad(conn, gn, freq_id=1, chrom="1", pos=1000, af_global=0.2)
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=gn)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    row = rows[0]
    assert row["most_severe_consequence"] is None
    assert row["impact"] is None
    assert row["cadd_phred"] is None
    assert row["alphamissense_class"] is None
    assert row["is_acmg_sf"] is None


# ---------------------------------------------------------------------------
# is_curated — ClinVar or PharmGKB only; CPIC excluded.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_db", "table", "expect_curated"),
    [
        ("clinvar", "clinvar_annotations", True),
        ("pharmgkb", "pharmgkb_annotations", True),
        ("gnomad", "gnomad_frequencies", False),
        ("gwas_catalog", "gwas_catalog_associations", False),
    ],
)
def test_is_curated_only_clinvar_and_pharmgkb(
    source_db: str,
    table: str,
    expect_curated: bool,  # noqa: FBT001
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """is_curated is TRUE only where ClinVar or PharmGKB contributed."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="5", pos=8000, ref="A", alt="G", rsid="rsZ")
        sv = _new_version(conn, source_db=source_db, version="v1", hash_char="a")
        if source_db == "clinvar":
            _seed_clinvar(conn, sv, clinvar_id=1, significance="Benign", chrom="5", pos=8000)
        elif source_db == "pharmgkb":
            _seed_pharmgkb(conn, sv, pharmgkb_id=1, rsid="rsZ", drug_name="d")
        elif source_db == "gnomad":
            _seed_gnomad(conn, sv, freq_id=1, chrom="5", pos=8000, af_global=0.2)
        else:
            _seed_gwas(conn, sv, association_id=1, rsid="rsZ", trait_name="T", p_value=1e-6)
        _activate(conn, source_db=source_db, table=table, sv_id=sv)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1
    assert rows[0]["is_curated"] is expect_curated


def test_cpic_does_not_affect_index(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A CPIC guideline (gene+drug grain) adds no rows and never flips is_curated."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        gn = _new_version(conn, source_db="gnomad", version="gn1", hash_char="c")
        _seed_gnomad(conn, gn, freq_id=1, chrom="1", pos=1000, af_global=0.2)
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=gn)
        cp = _new_version(conn, source_db="cpic", version="cp1", hash_char="e")
        _seed_cpic(conn, cp, guideline_id=1, gene_symbol="CYP2C19", drug_name="clopidogrel")
        _activate(conn, source_db="cpic", table="cpic_guidelines", sv_id=cp)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1  # only the gnomAD variant; CPIC adds nothing
    assert rows[0]["is_curated"] is False  # gnomAD-only, CPIC does not curate


# ---------------------------------------------------------------------------
# Version filtering — only current-pointer rows.
# ---------------------------------------------------------------------------


def test_only_current_version_rows_are_indexed(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Rows under a superseded ClinVar version are excluded from the index."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        old = _new_version(conn, source_db="clinvar", version="old", hash_char="a")
        _seed_clinvar(conn, old, clinvar_id=1, significance="Pathogenic", conditions=["Old cond"])
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=old)
        # Newer release: same variant, different significance, then flip pointer.
        new = _new_version(conn, source_db="clinvar", version="new", hash_char="b")
        _seed_clinvar(conn, new, clinvar_id=2, significance="Benign", conditions=["New cond"])
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=new)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert len(rows) == 1
    row = rows[0]
    # Only the current (Benign / New cond) release contributes.
    assert row["clinvar_significance"] == "Benign"
    assert row["clinvar_count"] == 1
    assert row["clinvar_conditions"] == ["New cond"]


# ---------------------------------------------------------------------------
# Wholesale replace / idempotence.
# ---------------------------------------------------------------------------


def test_rerun_is_idempotent(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Running twice against the same state yields identical rows, no duplicates."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        cv = _new_version(conn, source_db="clinvar", version="cv1", hash_char="a")
        _seed_clinvar(conn, cv, clinvar_id=1, significance="Pathogenic")
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        first = refresh_index(conn)
        second = refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert first.row_count == 1
    assert second.row_count == 1
    assert len(rows) == 1  # PK + wholesale replace ⇒ no stale duplicate


def test_reflip_drops_old_version_rows(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Mutate a source + reflip pointer + re-run → old-version rows are gone."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        _seed_variant(conn, 2, chrom="1", pos=2000, ref="A", alt="G", rsid="rs2")
        # v1: only variant 1 has a gnomAD row.
        v1 = _new_version(conn, source_db="gnomad", version="g1", hash_char="a")
        _seed_gnomad(conn, v1, freq_id=1, chrom="1", pos=1000, af_global=0.2)
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=v1)
        refresh_index(conn)
        first_ids = {r["variant_id"] for r in _fetch_index_rows(conn)}

        # v2: only variant 2 has a gnomAD row; flip pointer; re-run.
        v2 = _new_version(conn, source_db="gnomad", version="g2", hash_char="b")
        _seed_gnomad(conn, v2, freq_id=2, chrom="1", pos=2000, af_global=0.3)
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=v2)
        refresh_index(conn)
        second_ids = {r["variant_id"] for r in _fetch_index_rows(conn)}

    assert first_ids == {1}
    assert second_ids == {2}  # variant 1 dropped, variant 2 added


# ---------------------------------------------------------------------------
# Empty / partial states.
# ---------------------------------------------------------------------------


def test_empty_variants_master_builds_zero_rows(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """No variants → row_count 0, no error, even with sources loaded."""
    init_databases()
    with duckdb_connection() as conn:
        cv = _new_version(conn, source_db="clinvar", version="cv1", hash_char="a")
        _seed_clinvar(conn, cv, clinvar_id=1, significance="Pathogenic")
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        result = refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert result.row_count == 0
    assert rows == []


def test_no_sources_loaded_builds_zero_rows(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Variants present but no annotation source loaded → empty index, no error."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, rsid="rs1")
        result = refresh_index(conn)
        rows = _fetch_index_rows(conn)

    assert result.row_count == 0
    assert result.refresh_versions == {}
    assert rows == []


def test_only_gnomad_loaded_partial_build(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A single loaded source still builds; refresh_versions names only that source."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G")
        gn = _new_version(conn, source_db="gnomad", version="4.1.1", hash_char="c")
        _seed_gnomad(conn, gn, freq_id=1, chrom="1", pos=1000, af_global=0.2)
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=gn)
        result = refresh_index(conn)

    assert result.row_count == 1
    assert result.gnomad_matches == 1
    assert result.refresh_versions == {"gnomad": "4.1.1"}


# ---------------------------------------------------------------------------
# Multiallelic — rsid fans out to both splits; coords pin to exactly one.
# ---------------------------------------------------------------------------


def test_multiallelic_rsid_fans_out_coord_pins(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Two biallelic splits share an rsid: GWAS hits both, ClinVar coord hits one."""
    init_databases()
    with duckdb_connection() as conn:
        # Same locus, same rsid, two alt alleles → two variants_master rows.
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        _seed_variant(conn, 2, chrom="1", pos=1000, ref="A", alt="T", rsid="rs1")
        gw = _new_version(conn, source_db="gwas_catalog", version="gw1", hash_char="b")
        _seed_gwas(conn, gw, association_id=1, rsid="rs1", trait_name="Height", p_value=1e-9)
        _activate(conn, source_db="gwas_catalog", table="gwas_catalog_associations", sv_id=gw)
        cv = _new_version(conn, source_db="clinvar", version="cv1", hash_char="a")
        # ClinVar row matches only the A>G split.
        _seed_clinvar(
            conn, cv, clinvar_id=1, significance="Pathogenic", chrom="1", pos=1000, ref="A", alt="G"
        )
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        refresh_index(conn)
        rows = _fetch_index_rows(conn)

    by_id = {r["variant_id"]: r for r in rows}
    assert set(by_id) == {1, 2}
    # GWAS trait attaches to both splits (locus-level evidence).
    assert by_id[1]["gwas_trait_count"] == 1
    assert by_id[2]["gwas_trait_count"] == 1
    # ClinVar coord pins to exactly the A>G split.
    assert by_id[1]["clinvar_significance"] == "Pathogenic"
    assert by_id[2]["clinvar_significance"] is None
    assert by_id[1]["clinvar_count"] == 1
    assert by_id[2]["clinvar_count"] == 0


# ---------------------------------------------------------------------------
# Result object + provenance.
# ---------------------------------------------------------------------------


def test_result_counts_and_versions(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """IndexRefreshResult carries per-source match counts + the version snapshot."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        cv = _new_version(conn, source_db="clinvar", version="cv-v", hash_char="a")
        _seed_clinvar(conn, cv, clinvar_id=1, significance="Pathogenic")
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        pg = _new_version(conn, source_db="pharmgkb", version="pg-v", hash_char="d")
        _seed_pharmgkb(conn, pg, pharmgkb_id=1, rsid="rs1", drug_name="warfarin")
        _activate(conn, source_db="pharmgkb", table="pharmgkb_annotations", sv_id=pg)
        result = refresh_index(conn)

    assert isinstance(result, IndexRefreshResult)
    assert result.row_count == 1
    assert result.clinvar_matches == 1
    assert result.pharmgkb_matches == 1
    assert result.gnomad_matches == 0
    assert result.gwas_matches == 0
    assert result.curated_count == 1
    assert result.refresh_versions == {"clinvar": "cv-v", "pharmgkb": "pg-v"}
    assert isinstance(result.elapsed_ms, int)
    assert result.elapsed_ms >= 0


def test_refresh_versions_stamped_identically_on_every_row(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The refresh_versions JSON is non-null and identical on every row."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        _seed_variant(conn, 2, chrom="1", pos=2000, ref="A", alt="G", rsid="rs2")
        cv = _new_version(conn, source_db="clinvar", version="cv-v", hash_char="a")
        _seed_clinvar(conn, cv, clinvar_id=1, significance="Pathogenic", chrom="1", pos=1000)
        _seed_clinvar(conn, cv, clinvar_id=2, significance="Benign", chrom="1", pos=2000)
        _activate(conn, source_db="clinvar", table="clinvar_annotations", sv_id=cv)
        refresh_index(conn)
        version_jsons = conn.execute(
            "SELECT DISTINCT refresh_versions FROM variant_annotations_index",
        ).fetchall()

    assert len(version_jsons) == 1  # identical on every row
    assert version_jsons[0][0] is not None
    assert '"clinvar"' in version_jsons[0][0]


# ---------------------------------------------------------------------------
# Connection ownership — refresh_index(None) opens + closes its own conn.
# ---------------------------------------------------------------------------


def test_refresh_index_opens_own_connection(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """With no conn passed, refresh_index opens its own and persists the build."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, 1, chrom="1", pos=1000, ref="A", alt="G")
        gn = _new_version(conn, source_db="gnomad", version="gn1", hash_char="c")
        _seed_gnomad(conn, gn, freq_id=1, chrom="1", pos=1000, af_global=0.2)
        _activate(conn, source_db="gnomad", table="gnomad_frequencies", sv_id=gn)

    # No connection argument → refresh_index manages its own.
    result = refresh_index()
    assert result.row_count == 1

    with duckdb_connection(read_only=True) as conn:
        rows = _fetch_index_rows(conn)
    assert len(rows) == 1
