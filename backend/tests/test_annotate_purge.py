"""Tests for :mod:`genome.annotate.purge` (PR 9 / RM-12873bf — general superseded-row purge).

Authored **plan-blind** (Stage-2 independent oracle): every assertion is written from the
approved §5 test spec + the frozen `genome.annotate.purge` interface, never from the
implementation diff (function bodies / actual return values were not read).

Fixtures: every test runs against a FULL ``init_databases()`` schema (so ``annotation_sources``
+ ``genes``/``traits``/``pathways`` + all 14 FK children of ``annotation_source_versions``
exist). A minimal hand-built fixture that omitted ``annotation_sources`` would HIDE the headline
BinderException these tests exist to catch, so it is deliberately avoided. Versions are seeded
via ``insert_source_version`` and the pointer flipped via ``flip_to_new_version`` (or a direct
``annotation_sources`` UPSERT), mirroring ``test_strand_collapse.py``.

The big numbers in CLAUDE.md obs #4/#7 (gnomad svid8=4,467,370 / svid10=4,568,802 /
gnomad_matches=3,054,426 / genes svid=11 rows=1153) are the live anchors; the unit fixtures use
small synthetic counts that stand in for them and are referenced in comments.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import duckdb
import pytest
import structlog
from typer.testing import CliRunner

from genome.annotate.index_refresh import refresh_index
from genome.annotate.purge import (
    _FK_CHILDREN_WITHOUT_POINTER,
    _SOURCE_DB_TABLES,
    ActiveBuildAtRiskError,
    AmbiguousPartitionError,
    DanglingPointerError,
    PurgeNegativeControlError,
    PurgeResult,
    RegistryStillReferencedError,
    SourcePurgePlan,
    _fk_child_tables,
    compute_purge_plan,
    purge_superseded,
)
from genome.annotate.source_versions import insert_source_version
from genome.annotate.supersession import flip_to_new_version
from genome.cli import app
from genome.config import get_settings
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from duckdb import DuckDBPyConnection


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Restore structlog defaults so capture_logs doesn't leak between tests."""
    try:
        yield
    finally:
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Seeding helpers (kept local — mirrors the per-file helper style of the suite)
# ---------------------------------------------------------------------------


def _next_id(conn: DuckDBPyConnection, table: str, col: str) -> int:
    """``COALESCE(MAX(col), 0) + 1`` over ``table`` (surrogate-PK allocator)."""
    row = conn.execute(f"SELECT COALESCE(MAX({col}), 0) + 1 FROM {table}").fetchone()  # noqa: S608 — literal names
    return int(row[0]) if row is not None else 1


def _version_count(conn: DuckDBPyConnection, svid: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM annotation_source_versions WHERE source_version_id = ?",
        [svid],
    ).fetchone()
    return int(row[0]) if row is not None else -1


def _data_count(conn: DuckDBPyConnection, table: str, svid: int) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE source_version_id = ?",  # noqa: S608 — literal table names
        [svid],
    ).fetchone()
    return int(row[0]) if row is not None else -1


def _ts(month: int) -> datetime:
    """A naive 2026 timestamp on the given month — seeds ingested_at ordering only."""
    return datetime(2026, month, 1)  # noqa: DTZ001 — naive TIMESTAMP seed (column is tz-naive)


def _mk_version(
    conn: DuckDBPyConnection,
    source_db: str,
    *,
    version: str = "v1",
    when: datetime | None = None,
    record_count: int = 0,
) -> int:
    """Insert one ``annotation_source_versions`` row; optionally pin ``ingested_at``.

    ``ingested_at`` is pinned explicitly (not left to ``CURRENT_TIMESTAMP``) for the
    recency-ordering tests, since DuckDB's ``CURRENT_TIMESTAMP`` is fixed per transaction
    and would not separate same-connection inserts.
    """
    svid = insert_source_version(
        conn,
        source_db=source_db,
        version=version,
        source_url=None,
        source_file_hash="f" * 64,
        source_file_size=1,
        record_count=record_count,
    )
    if when is not None:
        conn.execute(
            "UPDATE annotation_source_versions SET ingested_at = ? WHERE source_version_id = ?",
            [when, svid],
        )
    return svid


def _flip(conn: DuckDBPyConnection, source: str, table: str, svid: int) -> None:
    flip_to_new_version(conn, source=source, table=table, new_source_version_id=svid)


def _seed_gnomad(  # noqa: PLR0913 — coordinate identity fields not collapsible
    conn: DuckDBPyConnection,
    svid: int,
    *,
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alt: str = "G",
    af: float = 0.2,
) -> None:
    fid = _next_id(conn, "gnomad_frequencies", "freq_id")
    conn.execute(
        """
        INSERT INTO gnomad_frequencies
            (freq_id, chrom, pos_grch38, ref_allele, alt_allele, af_global,
             source_version_id, retrieval_date)
        VALUES (?, ?::chromosome_enum, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [fid, chrom, pos, ref, alt, af, svid],
    )


def _seed_clinvar(conn: DuckDBPyConnection, svid: int) -> None:
    cid = _next_id(conn, "clinvar_annotations", "clinvar_id")
    conn.execute(
        """
        INSERT INTO clinvar_annotations (clinvar_id, source_version_id, retrieval_date)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        """,
        [cid, svid],
    )


def _seed_dbsnp(conn: DuckDBPyConnection, svid: int) -> None:
    did = _next_id(conn, "dbsnp_annotations", "dbsnp_id")
    conn.execute(
        """
        INSERT INTO dbsnp_annotations (dbsnp_id, rsid, source_version_id, retrieval_date)
        VALUES (?, 'rs1', ?, CURRENT_TIMESTAMP)
        """,
        [did, svid],
    )


def _seed_alias(conn: DuckDBPyConnection, svid: int) -> None:
    aid = _next_id(conn, "variant_aliases", "alias_id")
    conn.execute(
        """
        INSERT INTO variant_aliases
            (alias_id, alias_rsid, current_rsid, alias_type, source_version_id, retrieval_date)
        VALUES (?, 'rsOld', 'rsNew', 'merged', ?, CURRENT_TIMESTAMP)
        """,
        [aid, svid],
    )


def _seed_pgs_score(conn: DuckDBPyConnection, svid: int) -> None:
    rid = _next_id(conn, "pgs_catalog_scores", "score_record_id")
    conn.execute(
        """
        INSERT INTO pgs_catalog_scores (score_record_id, pgs_id, source_version_id, retrieval_date)
        VALUES (?, 'PGS000001', ?, CURRENT_TIMESTAMP)
        """,
        [rid, svid],
    )


def _seed_pgs_weight(conn: DuckDBPyConnection, svid: int) -> None:
    wid = _next_id(conn, "pgs_score_weights", "weight_id")
    conn.execute(
        """
        INSERT INTO pgs_score_weights (weight_id, pgs_id, weight, source_version_id)
        VALUES (?, 'PGS000001', 0.1, ?)
        """,
        [wid, svid],
    )


def _seed_genes(conn: DuckDBPyConnection, svid: int, *, symbol: str = "BRCA1") -> None:
    conn.execute(
        """
        INSERT INTO genes (gene_symbol, source_version_id, retrieval_date, is_acmg_sf)
        VALUES (?, ?, CURRENT_TIMESTAMP, TRUE)
        """,
        [symbol, svid],
    )


def _seed_variant(  # noqa: PLR0913 — variant identity fields not collapsible
    conn: DuckDBPyConnection,
    *,
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alt: str = "G",
    rsid: str | None = "rs1",
) -> int:
    vid = _next_id(conn, "variants_master", "variant_id")
    conn.execute(
        """
        INSERT INTO variants_master
            (variant_id, rsid, chrom, pos_grch38, ref_allele, alt_allele)
        VALUES (?, ?, ?::chromosome_enum, ?, ?, ?)
        """,
        [vid, rsid, chrom, pos, ref, alt],
    )
    return vid


def _seed_versions(  # noqa: PLR0913 — seeding knobs not collapsible
    conn: DuckDBPyConnection,
    source_db: str,
    table: str,
    n: int,
    *,
    active_index: int,
    seeders: list[Callable[[DuckDBPyConnection, int], None]],
    data_indices: tuple[int, ...] | None = None,
) -> list[int]:
    """Seed ``n`` versions for ``source_db`` (ingested_at ascending), flip the pointer.

    ``data_indices`` selects which versions get data rows (default: all). A version with
    no data row stands in for an interrupted-deletion registry orphan.
    """
    if data_indices is None:
        data_indices = tuple(range(n))
    svids: list[int] = []
    for i in range(n):
        svid = _mk_version(conn, source_db, version=f"{source_db}-{i}", when=_ts(1 + i))
        if i in data_indices:
            for seed in seeders:
                seed(conn, svid)
        svids.append(svid)
    _flip(conn, source_db, table, svids[active_index])
    return svids


# ---------------------------------------------------------------------------
# Frozen interface constants (§5 item 2 companion — pins the maps; typo guard)
# ---------------------------------------------------------------------------


def test_source_db_tables_and_pointerless_constants() -> None:
    """``_SOURCE_DB_TABLES`` + ``_FK_CHILDREN_WITHOUT_POINTER`` match the frozen contract.

    from: plan §5 frozen interface (per-source table map + pointer-less children).
    """
    assert _SOURCE_DB_TABLES["clinvar"] == ("clinvar_annotations",)
    assert _SOURCE_DB_TABLES["gwas_catalog"] == ("gwas_catalog_associations",)
    assert _SOURCE_DB_TABLES["pharmgkb"] == ("pharmgkb_annotations",)
    assert _SOURCE_DB_TABLES["cpic"] == ("cpic_guidelines",)
    assert _SOURCE_DB_TABLES["gnomad"] == ("gnomad_frequencies",)
    assert _SOURCE_DB_TABLES["dbsnp"] == ("dbsnp_annotations", "variant_aliases")
    assert _SOURCE_DB_TABLES["pgs_catalog"] == ("pgs_catalog_scores", "pgs_score_weights")
    assert set(_SOURCE_DB_TABLES) == {
        "clinvar",
        "gwas_catalog",
        "pharmgkb",
        "cpic",
        "gnomad",
        "dbsnp",
        "pgs_catalog",
    }
    assert (
        frozenset(
            {"vep_consequences", "genes", "traits", "pathways"},
        )
        == _FK_CHILDREN_WITHOUT_POINTER
    )


def test_fk_child_coverage_is_exactly_14(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """``_fk_child_tables(conn)`` introspects exactly the 14 FK children, each with its column.

    The completeness drift guard: the introspected catalog set must equal the union of the
    frozen maps + ``annotation_sources``. A future 15th FK child not added to the maps trips
    this. ``annotation_sources`` references via ``current_source_version_id``; the other 13 via
    ``source_version_id``.

    from: plan §5 item 2 (§6: set(keys) == flatten(_SOURCE_DB_TABLES) | pointer-less | {asources}).
    """
    init_databases()
    expected: set[str] = set()
    for tables in _SOURCE_DB_TABLES.values():
        expected.update(tables)
    expected |= set(_FK_CHILDREN_WITHOUT_POINTER)
    expected.add("annotation_sources")

    with duckdb_connection() as conn:
        mapping = _fk_child_tables(conn)

    assert set(mapping) == expected
    assert len(mapping) == 14
    assert mapping["annotation_sources"] == "current_source_version_id"
    for child, column in mapping.items():
        if child != "annotation_sources":
            assert column == "source_version_id", child


# ---------------------------------------------------------------------------
# Partition (compute_purge_plan / SourcePurgePlan) — §5 items 1, 8
# ---------------------------------------------------------------------------


def test_recency_predicate_would_delete_active_but_pointer_guard_keeps_it(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The finding-015 trap: the active version is the OLDEST by ingested_at, a newer
    superseded version exists. A naive newest-by-ingested_at partition would put the active
    in deletable; the pointer guard keeps it.

    from: plan §5 item 1 (§6: active_id excluded from deletable; deletable == {middle}).
    """
    init_databases()
    with duckdb_connection() as conn:
        # active_index=0 → pointer targets the OLDEST (Jan); v2 is the newest (Mar) superseded.
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=0, seeders=[_seed_gnomad]
        )
        plan = compute_purge_plan(conn, "gnomad", keep=1)
    assert plan.active_id == s[0]  # pointer wins over recency
    assert s[0] not in plan.deletable_ids  # the headline guarantee
    assert plan.prior_id == s[2]  # newest superseded kept as the immediate prior
    assert set(plan.deletable_ids) == {s[1]}  # only the genuinely-old middle version


def test_partition_keep1_keeps_active_and_prior(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """pointer=v3 → active=v3, prior=v2, deletable=(v1,).

    from: plan §5 item 8 (§6: keep-1 keeps active + one prior).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
        plan = compute_purge_plan(conn, "gnomad", keep=1)
    assert plan.active_id == s[2]
    assert plan.prior_id == s[1]
    assert set(plan.deletable_ids) == {s[0]}


def test_partition_gnomad_shape_nothing_deletable(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """gnomad svid8(prior)+svid10(active), pointer=10 → deletable=() (the obs #4 shape).

    from: plan §5 item 8 (§6 anchor: two gnomad versions, keep-1 → nothing deletable).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 2, active_index=1, seeders=[_seed_gnomad]
        )
        plan = compute_purge_plan(conn, "gnomad", keep=1)
    assert plan.active_id == s[1]
    assert set(plan.deletable_ids) == set()


def test_partition_excludes_active_by_construction(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """compute_purge_plan never lists the active svid in deletable.

    from: plan §5 item 8 (§6: active excluded by construction).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
        plan = compute_purge_plan(conn, "gnomad", keep=1)
    assert plan.active_id == s[2]
    assert plan.active_id not in plan.deletable_ids


def test_active_in_deletable_raises(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partition that places the active svid in its own deletable set is rejected with
    ActiveBuildAtRiskError.

    The spec names the exception, not the trigger site; a fabricated plan (SimpleNamespace, so
    it can hold the illegal state) is fed through the public execute path where the plan is
    consumed.

    from: plan §5 item 8 (§6: active-in-deletable → ActiveBuildAtRiskError).
    """
    init_databases()
    with duckdb_connection() as conn:
        active = _mk_version(conn, "gnomad", version="4.1.active", when=_ts(3))
        _seed_gnomad(conn, active)
        _flip(conn, "gnomad", "gnomad_frequencies", active)
    buggy = SimpleNamespace(
        source_db="gnomad",
        active_id=active,
        prior_id=None,
        deletable_ids=(active,),  # active in its own deletable — the at-risk state
        tables=("gnomad_frequencies",),
        row_counts={"gnomad_frequencies": 1},
    )
    monkeypatch.setattr("genome.annotate.purge.compute_purge_plan", lambda *a, **k: buggy)  # noqa: ARG005
    with pytest.raises(ActiveBuildAtRiskError):
        purge_superseded(execute=True, source="gnomad", no_backup=True)


def test_keep_flag_semantics(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """keep=1 retains active + 1 prior; keep=2 retains active + 2 priors (nothing deletable).

    from: plan §5 item 17 (keep flag semantics).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
        keep1 = compute_purge_plan(conn, "gnomad", keep=1)
        keep2 = compute_purge_plan(conn, "gnomad", keep=2)
    assert set(keep1.deletable_ids) == {s[0]}
    assert set(keep2.deletable_ids) == set()


def test_dangling_pointer_aborts(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A source-db-mismatched pointer (FK-valid, semantically dangling) aborts with
    DanglingPointerError.

    Resolution of the plan gap the original skip flagged: a pointer to a NONEXISTENT registry
    row is FK-un-constructible, but the ``current_source_version_id`` FK does not pin
    ``source_db`` — so a gnomad pointer -> a *dbsnp* version id is FK-valid yet dangling (that
    id holds no gnomad data, so a naive partition would route the true active build into
    deletable). The impl now fails closed on it, making DanglingPointerError reachable through
    the public surface.

    from: plan §5 item 9 (§6: dangling pointer → DanglingPointerError).
    """
    init_databases()
    with duckdb_connection() as conn:
        gnomad_svid = _mk_version(conn, "gnomad", version="4.1")
        _seed_gnomad(conn, gnomad_svid)
        dbsnp_svid = _mk_version(conn, "dbsnp", version="157")
        # FK-valid (the id exists) but the version belongs to dbsnp, not gnomad.
        conn.execute(
            "INSERT INTO annotation_sources (source_db, current_source_version_id) "
            "VALUES ('gnomad', ?)",
            [dbsnp_svid],
        )
        with pytest.raises(DanglingPointerError):
            compute_purge_plan(conn, "gnomad", keep=1)


def test_source_without_pointer_skipped_fail_closed(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A source with versions + data but no annotation_sources pointer is never targeted.

    The obs #7 hgnc/genes shape (genes seed deliberately flips no pointer). Even while the run
    actively purges a superseded gnomad version, the pointer-less hgnc version + genes rows are
    left untouched (fail-closed).

    from: plan §5 item 9 (§6: no-pointer source skipped, fail-closed).
    """
    init_databases()
    with duckdb_connection() as conn:
        hgnc_svid = _mk_version(conn, "hgnc", version="acmg_sf_v3.3+pgx_derived")
        _seed_genes(conn, hgnc_svid)  # no pointer flip — hgnc has no annotation_sources row
        g = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    result = purge_superseded(execute=True, source=None, no_backup=True)
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, g[0]) == 0  # the run did real work (gnomad purged)
        assert _version_count(conn, hgnc_svid) == 1  # pointer-less version never targeted
        assert _data_count(conn, "genes", hgnc_svid) == 1
    assert all(p.source_db != "hgnc" for p in result.plans)


# ---------------------------------------------------------------------------
# FK-safe two-transaction ordering + per-child guard — §5 items 2, 3, 5
# ---------------------------------------------------------------------------


def test_fk_safe_two_transaction_ordering(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A direct registry DELETE while data remains RAISES; data-first-then-registry succeeds.

    The DuckDB FK-on-DELETE behavior that motivates the two-transaction split (necessary AND
    sufficient).

    from: plan §5 item 5 (§6: registry-before-data raises; data-first succeeds).
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _mk_version(conn, "clinvar", version="2026_05")
        _seed_clinvar(conn, svid)
        # registry-first while the child row references it → DuckDB constraint error
        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
                [svid],
            )
        # data-first, then registry → succeeds
        conn.execute("DELETE FROM clinvar_annotations WHERE source_version_id = ?", [svid])
        conn.execute("DELETE FROM annotation_source_versions WHERE source_version_id = ?", [svid])
        assert _version_count(conn, svid) == 0
        assert _data_count(conn, "clinvar_annotations", svid) == 0


def test_guard_uses_per_child_column_no_binderexception(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The registry-delete guard queries annotation_sources via current_source_version_id.

    genes is populated (obs #7 svid=11 shape; synthetic id here). The guard iterates all 14 FK
    children, each by its OWN referencing column — a naive single-column guard would raise
    BinderException on ``annotation_sources`` (no ``source_version_id`` there). Completion
    without raising proves the per-child column is used.

    from: plan §5 item 3 (§6: guard returns 0 without BinderException).
    """
    init_databases()
    with duckdb_connection() as conn:
        hgnc_svid = _mk_version(conn, "hgnc", version="acmg_sf_v3.3+pgx_derived")
        _seed_genes(conn, hgnc_svid)
        g = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    result = purge_superseded(execute=True, source="gnomad", no_backup=True)
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, g[0]) == 0  # purged, no BinderException
        assert _data_count(conn, "genes", hgnc_svid) == 1  # genes untouched
    assert result.registry_rows_deleted == 1


# ---------------------------------------------------------------------------
# Two-table sources (dbsnp, pgs_catalog) + all-children block — §5 items 4, 10, 14
# ---------------------------------------------------------------------------


def test_dbsnp_two_table_unit(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A superseded dbsnp svid with rows in BOTH dbsnp_annotations and variant_aliases:
    both deleted, active intact.

    from: plan §5 item 10 (§6: both dbsnp tables purged for the superseded svid).
    """
    init_databases()
    with duckdb_connection() as conn:
        d = _seed_versions(
            conn,
            "dbsnp",
            "dbsnp_annotations",
            3,
            active_index=2,
            seeders=[_seed_dbsnp, _seed_alias],
        )
    result = purge_superseded(execute=True, source="dbsnp", no_backup=True)
    with duckdb_connection(read_only=True) as conn:
        assert _data_count(conn, "dbsnp_annotations", d[0]) == 0
        assert _data_count(conn, "variant_aliases", d[0]) == 0
        assert _data_count(conn, "dbsnp_annotations", d[2]) == 1  # active intact
        assert _data_count(conn, "variant_aliases", d[2]) == 1
        assert _version_count(conn, d[0]) == 0
        assert _version_count(conn, d[2]) == 1
    assert result.registry_rows_deleted == 1
    assert result.data_rows_deleted == 2  # one row per dbsnp table


def test_pgs_catalog_registry_delete_happy_both_tables(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """pgs_catalog superseded svid with rows in BOTH score tables: both deleted, registry OK.

    from: plan §5 item 4 (§6: mapped → both tables deleted, then registry succeeds).
    """
    init_databases()
    with duckdb_connection() as conn:
        p = _seed_versions(
            conn,
            "pgs_catalog",
            "pgs_catalog_scores",
            3,
            active_index=2,
            seeders=[_seed_pgs_score, _seed_pgs_weight],
        )
    result = purge_superseded(execute=True, source="pgs_catalog", no_backup=True)
    with duckdb_connection(read_only=True) as conn:
        assert _data_count(conn, "pgs_catalog_scores", p[0]) == 0
        assert _data_count(conn, "pgs_score_weights", p[0]) == 0
        assert _data_count(conn, "pgs_catalog_scores", p[2]) == 1
        assert _data_count(conn, "pgs_score_weights", p[2]) == 1
        assert _version_count(conn, p[0]) == 0
    assert result.registry_rows_deleted == 1
    assert result.data_rows_deleted == 2


def test_pgs_catalog_registry_delete_blocked_when_weights_present(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With pgs_score_weights dropped from the map, the surviving weights rows make the
    all-children guard raise RegistryStillReferencedError BEFORE the registry DELETE — a clean
    abort, not a crash. The registry row + the un-mapped weights rows survive.

    from: plan §5 item 4 (§6: guard blocks registry delete; clean abort).
    """
    init_databases()
    with duckdb_connection() as conn:
        p = _seed_versions(
            conn,
            "pgs_catalog",
            "pgs_catalog_scores",
            3,
            active_index=2,
            seeders=[_seed_pgs_score, _seed_pgs_weight],
        )
    monkeypatch.setattr(
        "genome.annotate.purge._SOURCE_DB_TABLES",
        {**_SOURCE_DB_TABLES, "pgs_catalog": ("pgs_catalog_scores",)},
    )
    with pytest.raises(RegistryStillReferencedError):
        purge_superseded(execute=True, source="pgs_catalog", no_backup=True)
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, p[0]) == 1  # registry row NOT deleted (clean abort)
        assert _data_count(conn, "pgs_score_weights", p[0]) == 1  # the lingering child rows


def test_count_guard_blocks_registry_delete(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the data DELETE a no-op for one of a two-table source's tables (drop variant_aliases
    from the map) so rows remain → the count guard raises RegistryStillReferencedError before the
    registry DELETE.

    from: plan §5 item 14 (§6: count guard fires before registry delete when rows remain).
    """
    init_databases()
    with duckdb_connection() as conn:
        d = _seed_versions(
            conn,
            "dbsnp",
            "dbsnp_annotations",
            3,
            active_index=2,
            seeders=[_seed_dbsnp, _seed_alias],
        )
    monkeypatch.setattr(
        "genome.annotate.purge._SOURCE_DB_TABLES",
        {**_SOURCE_DB_TABLES, "dbsnp": ("dbsnp_annotations",)},
    )
    with pytest.raises(RegistryStillReferencedError):
        purge_superseded(execute=True, source="dbsnp", no_backup=True)
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, d[0]) == 1  # registry row not deleted
        assert _data_count(conn, "variant_aliases", d[0]) == 1  # lingering child rows


# ---------------------------------------------------------------------------
# Active-protection belts (TX1 belt + post-delete negative control) — §5 items 6, 14
# ---------------------------------------------------------------------------


def test_tx1_data_delete_has_active_belt(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A buggy partition that lists the active svid as deletable still deletes ZERO active rows.

    The ``AND source_version_id <> :active_id`` belt is defense-in-depth behind the RAIL #2
    pre-flight ``active ∉ deletable`` assert (which, with this SimpleNamespace, short-circuits
    first). The test pins only the invariant the belt and that assert jointly guarantee —
    active data survives — without asserting which layer fires, so it stays valid whichever
    one trips.

    from: plan §5 item 6 (§6: zero active rows deleted under a buggy partition).
    """
    init_databases()
    with duckdb_connection() as conn:
        active = _mk_version(conn, "gnomad", version="4.1.active", when=_ts(3))
        _seed_gnomad(conn, active)
        _seed_gnomad(conn, active)  # two active rows
        _flip(conn, "gnomad", "gnomad_frequencies", active)
    buggy = SimpleNamespace(
        source_db="gnomad",
        active_id=active,
        prior_id=None,
        deletable_ids=(active,),  # the bug: active listed as deletable
        tables=("gnomad_frequencies",),
        row_counts={"gnomad_frequencies": 2},
    )
    monkeypatch.setattr("genome.annotate.purge.compute_purge_plan", lambda *a, **k: buggy)  # noqa: ARG005
    with pytest.raises(
        (ActiveBuildAtRiskError, RegistryStillReferencedError, PurgeNegativeControlError)
    ):
        purge_superseded(execute=True, source="gnomad", no_backup=True)
    with duckdb_connection(read_only=True) as conn:
        assert _data_count(conn, "gnomad_frequencies", active) == 2  # belt held


def test_post_delete_assert_active_survives_fires(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force an active-touching delete → PurgeNegativeControlError.

    The negative control is the last line of defense behind the TX1 belt + the registry guard,
    which both shield the active version from the purge's *own* deletes — so the only way to make
    active rows vanish during the run is to inject the loss externally. The one public mutation
    seam is the pre-mutation snapshot: the ``take_snapshot`` spy deletes the active version's data
    via a separate connection mid-run, and the post-delete control (which reads the TRUE active
    via the pointer) catches that active rows did not survive.

    from: plan §5 item 14 (§6: active-touching delete → PurgeNegativeControlError).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    active = s[2]

    def _evil_snapshot(*_args: object, **_kwargs: object) -> None:
        # Simulate active-row corruption concurrent with the purge (external to the purge's
        # own belt-guarded deletes).
        with duckdb_connection() as corruptor:
            corruptor.execute(
                "DELETE FROM gnomad_frequencies WHERE source_version_id = ?",
                [active],
            )

    monkeypatch.setattr("genome.annotate.purge.take_snapshot", _evil_snapshot, raising=False)
    with pytest.raises(PurgeNegativeControlError):
        purge_superseded(execute=True, source="gnomad", no_backup=False)


# ---------------------------------------------------------------------------
# Orphan self-heal sweep — §5 item 7
# ---------------------------------------------------------------------------


def test_orphan_self_heal_sweeps_targeted_zero_row_registry(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A registry row with zero data (a TX1-commit/TX2-skipped remnant) is swept by the run,
    even though the plain keep-1 partition skips it (the data_bearing>0 filter).

    from: plan §5 item 7 (§6: orphan_rows_swept removes it; the plain partition would not).
    """
    init_databases()
    with duckdb_connection() as conn:
        # data only on v1 + v2 → v0 is a zero-data registry orphan.
        s = _seed_versions(
            conn,
            "gnomad",
            "gnomad_frequencies",
            3,
            active_index=2,
            seeders=[_seed_gnomad],
            data_indices=(1, 2),
        )
        plan = compute_purge_plan(conn, "gnomad", keep=1)
        assert s[0] not in plan.deletable_ids  # the data_bearing>0 filter skips the orphan
    result = purge_superseded(execute=True, source="gnomad", no_backup=True)
    assert result.orphan_rows_swept >= 1
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, s[0]) == 0  # orphan registry row swept
        assert _version_count(conn, s[1]) == 1  # prior intact
        assert _version_count(conn, s[2]) == 1  # active intact


# ---------------------------------------------------------------------------
# Dry-run gate + execute outcomes — §5 items 11, 12
# ---------------------------------------------------------------------------


def test_dry_run_default_no_mutation(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Default (execute omitted) mutates nothing but the read-only probe still reports deletable.

    from: plan §5 item 11 (§6: dry-run default, no mutation, deletable reported).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    result = purge_superseded(source="gnomad")  # execute defaults to False
    assert result.executed is False
    assert result.data_rows_deleted == 0
    assert result.registry_rows_deleted == 0
    assert result.backup_path is None
    gplan = next(p for p in result.plans if p.source_db == "gnomad")
    assert set(gplan.deletable_ids) == {s[0]}
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, s[0]) == 1  # nothing mutated


def test_mandatory_dry_run_gate_reports_deletable_before_execute(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """The execute path runs the read-only probe first (the deletable is reported in plans),
    then mutates.

    from: plan §5 item 11 (§6: probe runs before mutation; deletable surfaced).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    result = purge_superseded(execute=True, source="gnomad", no_backup=True)
    gplan = next(p for p in result.plans if p.source_db == "gnomad")
    assert set(gplan.deletable_ids) == {s[0]}  # the probe ran and surfaced it
    assert result.executed is True
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, s[0]) == 0  # the mutation followed


def test_execute_keep1_is_noop_when_only_one_superseded(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Two versions (active + one superseded), keep=1 → noop: negative_control_ok, backup skipped.

    from: plan §5 item 12 (§6: keep-1 noop; nothing deleted; backup skipped).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 2, active_index=1, seeders=[_seed_gnomad]
        )
    result = purge_superseded(execute=True, source="gnomad", no_backup=False)
    # NB: `executed` is intentionally NOT asserted — §6 pins only the no-op outcomes below, and
    # the result field tracks whether mutation actually occurred (not whether --execute was set).
    assert result.data_rows_deleted == 0
    assert result.registry_rows_deleted == 0
    assert result.negative_control_ok is True
    assert result.backup_path is None  # snapshot skipped — nothing to delete
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, s[0]) == 1
        assert _version_count(conn, s[1]) == 1


def test_execute_keep1_deletes_only_third_version(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Three versions, keep=1 → only the oldest (third) version is deleted; active + prior survive.

    from: plan §5 item 12 (§6: only the third version deleted).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    result = purge_superseded(execute=True, source="gnomad", no_backup=True)
    assert result.executed is True
    assert result.registry_rows_deleted == 1
    assert result.data_rows_deleted == 1
    assert result.negative_control_ok is True
    assert result.pointer_unchanged is True
    assert result.active_rows_unchanged is True
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, s[0]) == 0  # deleted
        assert _data_count(conn, "gnomad_frequencies", s[0]) == 0
        assert _version_count(conn, s[1]) == 1  # prior survives
        assert _version_count(conn, s[2]) == 1  # active survives
        assert _data_count(conn, "gnomad_frequencies", s[2]) == 1


# ---------------------------------------------------------------------------
# Snapshot: pre-mutation, conn-closed-first, restore round-trip — §5 items 13, 15
# ---------------------------------------------------------------------------


def test_close_conn_before_snapshot(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    """The compute connection is closed before take_snapshot is invoked (no open-writer conflict),
    and the snapshot is pre-mutation.

    The spy opens a fresh ``read_only`` connection — which DuckDB refuses while a writer is open
    (verified: ConnectionException). Success therefore proves the writer was closed first; the
    pre-mutation count (all three versions still present) proves the snapshot precedes the delete.

    Note (resolved spec ambiguity): the frozen interface does not name the snapshot seam; this
    patches ``genome.annotate.purge.take_snapshot`` (patch-where-used).

    from: plan §5 item 13 (§6: conn closed before snapshot; snapshot pre-mutation).
    """
    from pathlib import Path  # noqa: PLC0415 — local to keep the import surface minimal

    init_databases()
    with duckdb_connection() as conn:
        _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    fake_backup = Path(str(tmp_path)) / "purge_snapshot.duckdb"
    fake_backup.write_bytes(b"snapshot")
    probe: dict[str, object] = {"called": 0}

    def _spy(*_args: object, **_kwargs: object) -> Path:
        probe["called"] = int(probe["called"]) + 1  # type: ignore[call-overload]
        try:
            with duckdb_connection(read_only=True) as ro:
                row = ro.execute(
                    "SELECT COUNT(*) FROM annotation_source_versions WHERE source_db = 'gnomad'",
                ).fetchone()
            probe["pre_count"] = int(row[0]) if row is not None else -1
            probe["ro_open_ok"] = True
        except duckdb.Error as exc:
            probe["ro_open_ok"] = False
            probe["err"] = repr(exc)
        return fake_backup

    monkeypatch.setattr("genome.annotate.purge.take_snapshot", _spy, raising=False)
    result = purge_superseded(execute=True, source="gnomad", no_backup=False)

    assert probe["called"] >= 1  # snapshot taken on execute
    assert probe["ro_open_ok"] is True  # compute conn closed → no writer conflict at snapshot time
    assert probe["pre_count"] == 3  # snapshot is pre-mutation (all 3 versions still present)
    assert result.backup_path == fake_backup  # spy's path wired into the result


def test_snapshot_taken_on_execute(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """An execute run with a deletable version writes a real, purge-labelled snapshot file.

    from: plan §5 item 15 (§6: snapshot taken on execute; purge-meaningful label).
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    result = purge_superseded(execute=True, source="gnomad", no_backup=False)
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert "purge" in str(result.backup_path).lower()  # purge-meaningful snapshot location/label


def test_no_backup_skips_snapshot(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """--no-backup → no snapshot file, backup_path is None.

    from: plan §5 item 15 (§6: no-backup skips snapshot).
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    result = purge_superseded(execute=True, source="gnomad", no_backup=True)
    assert result.backup_path is None


def test_snapshot_skipped_when_nothing_to_delete(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """execute=True but nothing deletable → snapshot skipped (backup_path None).

    from: plan §5 item 15 (§6: snapshot skipped when nothing to delete).
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 2, active_index=1, seeders=[_seed_gnomad]
        )
    result = purge_superseded(execute=True, source="gnomad", no_backup=False)
    assert result.backup_path is None
    assert result.data_rows_deleted == 0
    assert result.negative_control_ok is True


def test_restore_roundtrip(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Snapshot → mutate → copy the .bak back → pre-state restored.

    from: plan §5 item 15 (§6: restore round-trip recovers the pre-purge state).
    """
    init_databases()
    with duckdb_connection() as conn:
        s = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    result = purge_superseded(execute=True, source="gnomad", no_backup=False)
    assert result.backup_path is not None
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, s[0]) == 0  # mutated: oldest version purged

    settings = get_settings()
    shutil.copy2(result.backup_path, settings.genome_duckdb_path)
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, s[0]) == 1  # pre-state restored


# ---------------------------------------------------------------------------
# Paired deletion + refresh-index anchor — §5 item 16
# ---------------------------------------------------------------------------


def test_paired_deletion_then_refresh_index_anchor(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Build the index from the active pointer, --execute deleting a NON-active gnomad version,
    then rebuild the index: gnomad_matches reproduces its seeded value.

    The rebuild proof has teeth only paired with a real deletion. Synthetic gnomad_matches=1
    stands in for the live anchor 3,054,426 (CLAUDE.md obs #4).

    from: plan §5 item 16 (§6: gnomad_matches re-checked after a paired deletion).
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_variant(conn, chrom="1", pos=1000, ref="A", alt="G", rsid="rs1")
        v1 = _mk_version(conn, "gnomad", version="4.1.0", when=_ts(1))
        _seed_gnomad(conn, v1, chrom="2", pos=2000, ref="C", alt="T")  # non-active, no match
        v2 = _mk_version(conn, "gnomad", version="4.1.1", when=_ts(2))
        _seed_gnomad(conn, v2, chrom="3", pos=3000, ref="G", alt="A")  # non-active, no match
        v3 = _mk_version(conn, "gnomad", version="4.1.2", when=_ts(3))
        _seed_gnomad(conn, v3, chrom="1", pos=1000, ref="A", alt="G", af=0.2)  # active, matches
        _flip(conn, "gnomad", "gnomad_frequencies", v3)

    before = refresh_index()
    assert before.gnomad_matches == 1  # seeded value (live anchor analog: 3,054,426)

    result = purge_superseded(execute=True, source="gnomad", no_backup=True)
    assert result.executed is True
    assert result.registry_rows_deleted == 1  # v1 (non-active) deleted
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, v1) == 0
        assert _version_count(conn, v3) == 1  # active intact

    after = refresh_index()
    assert after.gnomad_matches == before.gnomad_matches == 1  # active-pointer index unchanged


# ---------------------------------------------------------------------------
# Source filter + CLI — §5 item 17
# ---------------------------------------------------------------------------


def test_source_filter(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """--source gnomad purges only gnomad; clinvar's deletable version is protected.

    from: plan §5 item 17 (source filter).
    """
    init_databases()
    with duckdb_connection() as conn:
        g = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
        c = _seed_versions(
            conn, "clinvar", "clinvar_annotations", 3, active_index=2, seeders=[_seed_clinvar]
        )
    result = purge_superseded(execute=True, source="gnomad", no_backup=True)
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, g[0]) == 0  # gnomad deletable removed
        assert _version_count(conn, c[0]) == 1  # clinvar deletable protected by the filter
    assert all(p.source_db == "gnomad" for p in result.plans)


def test_cli_no_flags_dry_run_exits_zero_no_mutation(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """No flags → exit 0, a dry-run marker, and zero mutation (dry-run is the default).

    from: plan §5 item 17 (CLI smoke: dry-run default).
    """
    init_databases()
    with duckdb_connection() as conn:
        g = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "purge-superseded"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, g[0]) == 1  # nothing mutated


def test_cli_execute_prints_negative_control_and_orphan(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """--execute wires execute=True and echoes negative_control + orphan_rows_swept.

    from: plan §5 item 17 (CLI smoke: --execute echoes summary, mutates).
    """
    init_databases()
    with duckdb_connection() as conn:
        g = _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 3, active_index=2, seeders=[_seed_gnomad]
        )
    runner = CliRunner()
    result = runner.invoke(
        app, ["annotate", "purge-superseded", "--execute", "--source", "gnomad", "--no-backup"]
    )
    assert result.exit_code == 0, result.output
    assert "negative_control" in result.output
    assert "orphan_rows_swept" in result.output
    with duckdb_connection(read_only=True) as conn:
        assert _version_count(conn, g[0]) == 0  # execute=True wired → mutation happened


def test_cli_registry_still_referenced_exits_2(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RegistryStillReferencedError path surfaces as CLI exit code 2.

    from: plan §5 item 17 (CLI: guard-blocked path → exit 2).
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_versions(
            conn,
            "dbsnp",
            "dbsnp_annotations",
            3,
            active_index=2,
            seeders=[_seed_dbsnp, _seed_alias],
        )
    monkeypatch.setattr(
        "genome.annotate.purge._SOURCE_DB_TABLES",
        {**_SOURCE_DB_TABLES, "dbsnp": ("dbsnp_annotations",)},
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["annotate", "purge-superseded", "--execute", "--source", "dbsnp", "--no-backup"]
    )
    assert result.exit_code == 2, result.output


def test_cli_dangling_pointer_exits_2(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """A DanglingPointerError (source-db-mismatched pointer) surfaces as a clean exit 2.

    Realistic corruption — a failed refresh leaving a gnomad pointer -> a dbsnp version id; the
    CLI must convert it to exit 2, not a raw DanglingPointerError traceback.

    from: review fix 2 (CLI catches every PurgeError subclass).
    """
    init_databases()
    with duckdb_connection() as conn:
        gnomad_svid = _mk_version(conn, "gnomad", version="4.1")
        _seed_gnomad(conn, gnomad_svid)
        dbsnp_svid = _mk_version(conn, "dbsnp", version="157")
        conn.execute(
            "INSERT INTO annotation_sources (source_db, current_source_version_id) "
            "VALUES ('gnomad', ?)",
            [dbsnp_svid],
        )
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "purge-superseded", "--source", "gnomad"])
    assert result.exit_code == 2, result.output
    assert not isinstance(result.exception, DanglingPointerError)  # handled, not a raw traceback


def test_cli_active_build_at_risk_exits_2(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ActiveBuildAtRiskError (a buggy partition listing the active svid) surfaces as exit 2.

    from: review fix 2 (CLI catches every PurgeError subclass).
    """
    init_databases()
    with duckdb_connection() as conn:
        active = _mk_version(conn, "gnomad", version="4.1.active", when=_ts(3))
        _seed_gnomad(conn, active)
        _flip(conn, "gnomad", "gnomad_frequencies", active)
    buggy = SimpleNamespace(
        source_db="gnomad",
        active_id=active,
        prior_id=None,
        deletable_ids=(active,),
        tables=("gnomad_frequencies",),
        row_counts={"gnomad_frequencies": 1},
    )
    monkeypatch.setattr("genome.annotate.purge.compute_purge_plan", lambda *a, **k: buggy)  # noqa: ARG005
    runner = CliRunner()
    result = runner.invoke(
        app, ["annotate", "purge-superseded", "--execute", "--source", "gnomad", "--no-backup"]
    )
    assert result.exit_code == 2, result.output
    assert not isinstance(result.exception, ActiveBuildAtRiskError)  # handled cleanly


def test_cli_ambiguous_partition_exits_2(
    isolated_settings: dict[str, str],  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An AmbiguousPartitionError surfaces as exit 2.

    >1 annotation_sources pointer rows for one source_db is not naturally constructible
    (source_db is the PRIMARY KEY), so the partition is patched to raise it — validating the
    CLI's fail-closed catch for this PurgeError subclass.

    from: review fix 2 (CLI catches every PurgeError subclass).
    """
    init_databases()
    with duckdb_connection() as conn:
        _seed_versions(
            conn, "gnomad", "gnomad_frequencies", 2, active_index=1, seeders=[_seed_gnomad]
        )

    def _raise(*_a: object, **_k: object) -> SourcePurgePlan:
        msg = "gnomad: 2 annotation_sources pointer rows; expected exactly one"
        raise AmbiguousPartitionError(msg)

    monkeypatch.setattr("genome.annotate.purge.compute_purge_plan", _raise)
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "purge-superseded", "--source", "gnomad"])
    assert result.exit_code == 2, result.output


def test_cli_purge_superseded_in_help(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """The subcommand is discoverable in ``annotate --help``.

    from: plan §5 item 17 (CLI discoverability).
    """
    runner = CliRunner()
    result = runner.invoke(app, ["annotate", "--help"])
    assert result.exit_code == 0
    assert "purge-superseded" in result.output


# ---------------------------------------------------------------------------
# Frozen result/plan dataclasses (interface shape)
# ---------------------------------------------------------------------------


def test_source_purge_plan_is_frozen() -> None:
    """``SourcePurgePlan`` is frozen so a computed partition can't be mutated in place.

    from: plan §5 frozen interface (frozen+slots dataclass shape).
    """
    plan = SourcePurgePlan(
        source_db="gnomad",
        active_id=3,
        prior_id=2,
        deletable_ids=(1,),
        tables=("gnomad_frequencies",),
        row_counts={"gnomad_frequencies": 1},
    )
    with pytest.raises(AttributeError):
        plan.active_id = 9  # type: ignore[misc]


def test_purge_result_is_frozen() -> None:
    """``PurgeResult`` is frozen so the run summary can't be mutated after the fact.

    from: plan §5 frozen interface (frozen+slots dataclass shape).
    """
    result = PurgeResult(
        executed=False,
        plans=(),
        data_rows_deleted=0,
        registry_rows_deleted=0,
        orphan_rows_swept=0,
        backup_path=None,
        negative_control_ok=True,
        active_rows_unchanged=True,
        pointer_unchanged=True,
    )
    with pytest.raises(AttributeError):
        result.executed = True  # type: ignore[misc]
