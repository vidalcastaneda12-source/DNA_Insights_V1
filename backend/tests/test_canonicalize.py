"""Tests for :mod:`genome.annotate.canonicalize`.

Covers the mapping computation (ordering swap, hom-ref recover single-alt /
multi-alt, hom-alt recover, no-op exclusion), the collision-collapse with FK
re-point of ``genotype_calls.variant_id`` and survivor flag recompute, the
intrinsic idempotence (a second run reports zero deltas), the precondition
guards (dbSNP not loaded + Phase-6 table non-empty), and the pre-mutation
file snapshot + restore round-trip.

Shape mirrors :file:`test_annotate_index_refresh.py`: seed ``variants_master``
+ ``dbsnp_annotations`` under a flipped pointer, then call
``canonicalize_variants(conn=...)`` on the borrowed connection. The snapshot
path is exercised separately by calling ``take_snapshot`` directly against an
isolated DB file.
"""

from __future__ import annotations

import shutil
import stat
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from genome.annotate.canonicalize import (
    CanonicalizeResult,
    DbsnpNotLoadedError,
    DerivedTablesNotEmptyError,
    canonicalize_variants,
    take_snapshot,
)
from genome.annotate.source_versions import insert_source_version
from genome.annotate.supersession import flip_to_new_version
from genome.config import get_settings
from genome.db import duckdb_connection, init_databases

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

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
# Seeding helpers (kept small and local — the existing test_annotate_index_refresh
# helpers are not exported as a fixture).
# ---------------------------------------------------------------------------


def _seed_dbsnp_version(conn: DuckDBPyConnection, *, version: str = "157") -> int:
    """Allocate a dbSNP ``source_version_id`` and flip the pointer to it."""
    svid = insert_source_version(
        conn,
        source_db="dbsnp",
        version=version,
        source_url=None,
        source_file_hash="d" * 64,
        source_file_size=1,
        record_count=0,
    )
    flip_to_new_version(conn, source="dbsnp", table="dbsnp_annotations", new_source_version_id=svid)
    return svid


def _seed_variant(  # noqa: PLR0913 — variant identity fields not collapsible
    conn: DuckDBPyConnection,
    variant_id: int,
    *,
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alt: str = "G",
    rsid: str | None = None,
    variant_type: str = "SNV",
) -> None:
    """Insert one ``variants_master`` row with an explicit ``variant_id``."""
    conn.execute(
        """
        INSERT INTO variants_master
            (variant_id, rsid, chrom, pos_grch38, ref_allele, alt_allele, variant_type)
        VALUES (?, ?, ?, ?, ?, ?, ?::variant_type_enum)
        """,
        [variant_id, rsid, chrom, pos, ref, alt, variant_type],
    )


def _seed_variant_via_sequence(  # noqa: PLR0913 — variant identity fields not collapsible
    conn: DuckDBPyConnection,
    *,
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alt: str = "G",
    variant_type: str = "SNV",
) -> int:
    """Insert one ``variants_master`` row via the ``variant_id_seq`` DEFAULT.

    Mirrors the production ingest path (``writer.py`` / ``imputation.ingest``)
    which omits ``variant_id`` and relies on the sequence default — *this* is
    what keeps ``variant_id_seq`` in sync with reality, the condition the
    explicit-id :func:`_seed_variant` helper never reproduces. Returns the
    sequence-assigned ``variant_id``.
    """
    conn.execute(
        """
        INSERT INTO variants_master
            (rsid, chrom, pos_grch38, ref_allele, alt_allele, variant_type)
        VALUES (NULL, ?, ?, ?, ?, ?::variant_type_enum)
        """,
        [chrom, pos, ref, alt, variant_type],
    )
    row = conn.execute(
        "SELECT variant_id FROM variants_master WHERE pos_grch38 = ? "
        "AND ref_allele = ? AND alt_allele = ?",
        [pos, ref, alt],
    ).fetchone()
    assert row is not None
    return int(row[0])


def _seed_call(  # noqa: PLR0913 — call identity fields not collapsible
    conn: DuckDBPyConnection,
    call_id: int,
    variant_id: int,
    *,
    source: str = "23andme",
    allele_1: str = "A",
    allele_2: str = "G",
    is_no_call: bool = False,
) -> None:
    """Insert one ``genotype_calls`` row + a stub ``ingestion_runs`` row if needed."""
    # Ensure a stub ingestion_run exists (run_id=1 by convention here).
    existing = conn.execute(
        "SELECT COUNT(*) FROM ingestion_runs WHERE run_id = 1",
    ).fetchone()
    if existing is not None and existing[0] == 0:
        conn.execute(
            """
            INSERT INTO ingestion_runs
                (run_id, source, file_path, file_hash_sha256,
                 pipeline_version, status)
            VALUES (1, '23andme'::source_enum, '/tmp/x', 'h', 'test',
                    'completed'::ingestion_status_enum)
            """,
        )
    conn.execute(
        """
        INSERT INTO genotype_calls
            (call_id, variant_id, source, ingestion_run_id,
             allele_1, allele_2, is_no_call, is_active)
        VALUES (?, ?, ?::source_enum, 1, ?, ?, ?, TRUE)
        """,
        [call_id, variant_id, source, allele_1, allele_2, is_no_call],
    )


def _seed_dbsnp_annotation(  # noqa: PLR0913 — dbsnp identity fields not collapsible
    conn: DuckDBPyConnection,
    dbsnp_id: int,
    svid: int,
    *,
    rsid: str = "rs1",
    chrom: str = "1",
    pos: int = 1000,
    ref: str = "A",
    alts: Sequence[str] = ("G",),
    variant_class: str = "snv",
) -> None:
    """Insert one ``dbsnp_annotations`` row under ``source_version_id``."""
    conn.execute(
        """
        INSERT INTO dbsnp_annotations
            (dbsnp_id, rsid, chrom, pos_grch38, ref_allele, alt_alleles,
             variant_class, source_version_id, retrieval_date)
        VALUES (?, ?, ?::chromosome_enum, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        [dbsnp_id, rsid, chrom, pos, ref, list(alts), variant_class, svid],
    )


def _fetch_variants(conn: DuckDBPyConnection) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT variant_id, chrom, pos_grch38, ref_allele, alt_allele, has_genotyped_call"
        " FROM variants_master ORDER BY variant_id",
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def _fetch_calls(conn: DuckDBPyConnection) -> list[tuple[int, int, str]]:
    return [
        (int(r[0]), int(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT call_id, variant_id, source FROM genotype_calls ORDER BY call_id",
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def test_raises_when_no_dbsnp_loaded(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """No dbSNP pointer -> :class:`DbsnpNotLoadedError` before any mutation."""
    init_databases()
    with duckdb_connection() as conn, pytest.raises(DbsnpNotLoadedError):
        canonicalize_variants(conn, no_backup=True)


def test_refuses_when_derived_table_non_empty(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A non-empty Phase-6/7 variant_id-holding table -> refuse with explicit error.

    ``vep_consequences`` is the minimal-schema target (variant_id is BIGINT,
    no enforced FK per the DDL comment), so it's the cheapest precondition to
    fail. The error message must name the offending table + count for the
    operator to act on.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="A")
        # Seed one vep_consequences row (variant_id is plain BIGINT, no FK).
        conn.execute(
            """
            INSERT INTO vep_consequences
                (consequence_id, variant_id, source_version_id, retrieval_date)
            VALUES (1, 1, ?, CURRENT_TIMESTAMP)
            """,
            [svid],
        )
        with pytest.raises(DerivedTablesNotEmptyError) as exc_info:
            canonicalize_variants(conn, no_backup=True)
    assert "vep_consequences" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Genuine reorientation (the bulk swap fix)
# ---------------------------------------------------------------------------


def test_genuine_reorient_swap_to_dbsnp_orientation(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """variants_master stores (A,G) but dbSNP says ref=G, alt=A -> swap to (G,A)."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_call(conn, 1, 1, allele_1="A", allele_2="G")
        _seed_dbsnp_annotation(conn, 1, svid, ref="G", alts=("A",))
        result = canonicalize_variants(conn, no_backup=True)
        variants = _fetch_variants(conn)
    assert result.rows_reoriented == 1
    assert result.rows_recovered_hom_ref == 0
    assert result.rows_recovered_hom_alt == 0
    assert result.rows_collapsed == 0
    assert len(variants) == 1
    assert (variants[0]["ref_allele"], variants[0]["alt_allele"]) == ("G", "A")


def test_already_canonical_genuine_row_is_no_op(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """variants_master matches dbSNP orientation already -> excluded by no-op filter."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        result = canonicalize_variants(conn, no_backup=True)
        variants = _fetch_variants(conn)
    assert result.already_canonical is True
    assert result.rows_reoriented == 0
    assert (variants[0]["ref_allele"], variants[0]["alt_allele"]) == ("A", "G")


# ---------------------------------------------------------------------------
# Hom-only recovery
# ---------------------------------------------------------------------------


def test_hom_ref_recover_single_alt(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """ref==alt=='A', dbSNP ref=A alt=[G] -> (A,G), kind='hom_ref_recover'."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="A")
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        result = canonicalize_variants(conn, no_backup=True)
        variants = _fetch_variants(conn)
    assert result.rows_recovered_hom_ref == 1
    assert result.rows_recovered_hom_ref_multialt == 0
    assert (variants[0]["ref_allele"], variants[0]["alt_allele"]) == ("A", "G")


def test_hom_ref_recover_multi_alt_picks_alphabetically_smallest(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """ref==alt=='A', dbSNP alts=[T, C, G] -> picks 'C' (MIN), kind='multialt'."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="A")
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("T", "C", "G"))
        result = canonicalize_variants(conn, no_backup=True)
        variants = _fetch_variants(conn)
    assert result.rows_recovered_hom_ref == 0
    assert result.rows_recovered_hom_ref_multialt == 1
    assert (variants[0]["ref_allele"], variants[0]["alt_allele"]) == ("A", "C")


def test_hom_alt_recover(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """ref==alt=='G' (user observed G/G), dbSNP ref=A alts=[G] -> (A,G), dosage will be 2."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="G", alt="G")
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        result = canonicalize_variants(conn, no_backup=True)
        variants = _fetch_variants(conn)
    assert result.rows_recovered_hom_alt == 1
    assert (variants[0]["ref_allele"], variants[0]["alt_allele"]) == ("A", "G")


def test_hom_only_no_dbsnp_match_left_unchanged(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """ref==alt with no dbSNP record at the position -> not in canon_map, no change."""
    init_databases()
    with duckdb_connection() as conn:
        _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="A", pos=9999)
        # No dbSNP record at pos=9999.
        result = canonicalize_variants(conn, no_backup=True)
        variants = _fetch_variants(conn)
    assert result.already_canonical is True
    assert (variants[0]["ref_allele"], variants[0]["alt_allele"]) == ("A", "A")


# ---------------------------------------------------------------------------
# Collision / collapse / FK repoint
# ---------------------------------------------------------------------------


def test_collapse_repoints_genotype_calls_and_deletes_loser(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Hom-only (A,A) and Ancestry hom (G,G) at same pos both recover to (A,G):
    a single new survivor variant_id is INSERTed, both genotype_calls re-point
    to it, and both old variants_master rows are deleted.

    Per the module docstring: variant_id is NOT preserved for movers; we
    allocate a fresh id (collision-free via ``MAX(variant_id) + ROW_NUMBER()``)
    and re-point ``genotype_calls.variant_id`` to it. The assertion is on
    end-state shape, not specific id values.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="A")
        _seed_call(conn, 1, 1, source="23andme", allele_1="A", allele_2="A")
        _seed_variant(conn, 2, ref="G", alt="G")
        _seed_call(conn, 2, 2, source="ancestry", allele_1="G", allele_2="G")
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))

        result = canonicalize_variants(conn, no_backup=True)
        variants = _fetch_variants(conn)
        calls = _fetch_calls(conn)

    # Two movers collapsing into one survivor: net delta = -1 row.
    assert result.rows_collapsed == 1
    assert result.new_variant_ids_allocated == 1
    assert result.calls_repointed == 2
    assert result.rows_recovered_hom_ref == 1
    assert result.rows_recovered_hom_alt == 1
    assert len(variants) == 1
    survivor_id = variants[0]["variant_id"]
    assert survivor_id not in (1, 2)  # freshly allocated, not a reused old id
    assert (variants[0]["ref_allele"], variants[0]["alt_allele"]) == ("A", "G")
    # Both calls now point to the survivor.
    assert sorted(calls) == [
        (1, survivor_id, "23andme"),
        (2, survivor_id, "ancestry"),
    ]
    # Survivor's has_genotyped_call was recomputed to TRUE from the absorbed
    # call set (both calls are chip-sourced).
    assert variants[0]["has_genotyped_call"] is True


def test_collapse_unmatched_sibling(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """Hom-only (A,A) recovers to (A,G); an existing genuine (A,G) row sits there
    unmatched. The genuine row's variant_id is reused as the survivor (no new
    id allocated, no extra INSERT); the hom-only row's call re-points to it.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")  # genuine, already canonical
        _seed_call(conn, 1, 1, source="23andme", allele_1="A", allele_2="G")
        _seed_variant(conn, 2, ref="A", alt="A")  # hom-only, will recover to (A,G)
        _seed_call(conn, 2, 2, source="ancestry", allele_1="A", allele_2="A")
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))

        result = canonicalize_variants(conn, no_backup=True)
        variants = _fetch_variants(conn)
        calls = _fetch_calls(conn)

    # Existing sibling reused as survivor → no new id allocated; the single
    # mover (variant_id=2) collapses into variant_id=1.
    assert result.rows_collapsed == 1
    assert result.new_variant_ids_allocated == 0
    assert result.calls_repointed == 1
    assert len(variants) == 1
    assert variants[0]["variant_id"] == 1  # reused existing
    assert sorted(calls) == [(1, 1, "23andme"), (2, 1, "ancestry")]


# ---------------------------------------------------------------------------
# Idempotence + force
# ---------------------------------------------------------------------------


def test_second_run_reports_already_canonical(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """Re-running on canonical data hits the fast-path and reports zero deltas."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_dbsnp_annotation(conn, 1, svid, ref="G", alts=("A",))
        first = canonicalize_variants(conn, no_backup=True)
        assert first.rows_reoriented == 1
        assert first.already_canonical is False

        second = canonicalize_variants(conn, no_backup=True)
    assert second.already_canonical is True
    assert second.rows_reoriented == 0
    assert second.rows_changed == 0


def test_force_bypasses_fast_path_and_reports_zero_deltas(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """--force on already-canonical data does the full walk but writes nothing new."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="G")
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        result = canonicalize_variants(conn, force=True, no_backup=True)
    assert result.already_canonical is False
    assert result.rows_changed == 0
    assert result.rows_collapsed == 0


# ---------------------------------------------------------------------------
# Sequence re-sync (regression — the explicit-id allocator must not strand
# variant_id_seq behind the survivor ids it allocated)
# ---------------------------------------------------------------------------


def test_sequence_resynced_allows_default_path_insert(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """After a reorient allocates a survivor at MAX+1, the next default-path
    insert must not collide with it.

    The allocator assigns survivor ids explicitly as ``MAX(variant_id) +
    ROW_NUMBER()`` and never advances ``variant_id_seq``. Seeding the initial
    row *via the sequence default* (mirroring writer.py) is what puts the
    sequence in sync with reality — the production condition the explicit-id
    :func:`_seed_variant` helper never reproduces, which is exactly why the
    stale-sequence collision hid. Post-canonicalize,
    ``_resync_variant_id_sequence`` advances the sequence past the survivor id
    so a subsequent default-path (``nextval``) insert gets a fresh id instead
    of a duplicate-PK ``ConstraintException``.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        # Seed via the sequence DEFAULT so variant_id_seq tracks reality.
        seeded_id = _seed_variant_via_sequence(conn, ref="A", alt="G")
        _seed_call(conn, 1, seeded_id, allele_1="A", allele_2="G")
        # dbSNP says ref=G alt=[A] -> forces a reorient to (G,A). The only row at
        # the position is the mover, so a *new* survivor is allocated at MAX+1.
        _seed_dbsnp_annotation(conn, 1, svid, ref="G", alts=("A",))

        result = canonicalize_variants(conn, no_backup=True)
        assert result.rows_reoriented == 1
        assert result.new_variant_ids_allocated == 1

        post_max_row = conn.execute(
            "SELECT MAX(variant_id) FROM variants_master",
        ).fetchone()
        assert post_max_row is not None
        post_max = int(post_max_row[0])

        # The re-sync advanced the sequence so its next nextval exceeds the
        # survivor id (last_value >= post_max => next value == last_value+1).
        seq_row = conn.execute(
            "SELECT last_value FROM duckdb_sequences() WHERE sequence_name = 'variant_id_seq'",
        ).fetchone()
        assert seq_row is not None
        assert seq_row[0] is not None
        assert int(seq_row[0]) >= post_max

        # The default-path insert must succeed (pre-fix: the stale sequence
        # yields MAX+1 again — the id the new survivor already took — raising a
        # duplicate-PK ConstraintException here).
        new_id = _seed_variant_via_sequence(conn, pos=2000, ref="C", alt="T")
        assert new_id > post_max


# ---------------------------------------------------------------------------
# Downstream-clear behavior
# ---------------------------------------------------------------------------


def test_downstream_tables_cleared(isolated_settings: dict[str, str]) -> None:  # noqa: ARG001
    """consensus_genotypes / discrepancies / variant_annotations_index get DELETEd
    so the downstream re-runs rebuild from canonical variants_master.
    """
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="A")
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))
        # Seed a stub consensus row so we can prove it's cleared.
        conn.execute(
            """
            INSERT INTO consensus_genotypes
                (variant_id, consensus_method, resolution_rule)
            VALUES (1, 'single_source'::consensus_method_enum, 'consensus_v1')
            """,
        )
        before = conn.execute("SELECT COUNT(*) FROM consensus_genotypes").fetchone()
        assert before is not None
        assert before[0] == 1
        canonicalize_variants(conn, no_backup=True)
        after = conn.execute("SELECT COUNT(*) FROM consensus_genotypes").fetchone()
    assert after is not None
    assert after[0] == 0


# ---------------------------------------------------------------------------
# Snapshot / restore round-trip
# ---------------------------------------------------------------------------


def test_take_snapshot_roundtrip(
    isolated_settings: dict[str, str],  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """Snapshot a seeded DB, mutate, restore by file copy, query asserts pre-state."""
    init_databases()
    settings = get_settings()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="A")  # hom-only marker
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))

    backup = take_snapshot(
        settings.genome_duckdb_path,
        archive_root=tmp_path / "archive",
        dbsnp_version="157",
    )
    assert backup.exists()
    # Inherits the 0600 posture of the live DB file.
    assert stat.S_IMODE(backup.stat().st_mode) == (stat.S_IRUSR | stat.S_IWUSR)
    assert "dbsnp157" in backup.name

    # Mutate the live DB (canonicalize the hom-only marker).
    with duckdb_connection() as conn:
        result = canonicalize_variants(conn, no_backup=True)
    assert result.rows_recovered_hom_ref == 1

    with duckdb_connection() as conn:
        mutated = _fetch_variants(conn)
    assert (mutated[0]["ref_allele"], mutated[0]["alt_allele"]) == ("A", "G")

    # Restore by file copy.
    shutil.copy2(backup, settings.genome_duckdb_path)
    with duckdb_connection() as conn:
        restored = _fetch_variants(conn)
    assert (restored[0]["ref_allele"], restored[0]["alt_allele"]) == ("A", "A")


# ---------------------------------------------------------------------------
# Result shape + flag recompute when survivor absorbs an imputed loser
# ---------------------------------------------------------------------------


def test_survivor_flag_recompute_absorbs_imputed_call(
    isolated_settings: dict[str, str],  # noqa: ARG001
) -> None:
    """A survivor that absorbs a loser carrying an imputed call gets has_imputed_call=TRUE."""
    init_databases()
    with duckdb_connection() as conn:
        svid = _seed_dbsnp_version(conn)
        _seed_variant(conn, 1, ref="A", alt="A")
        _seed_call(conn, 1, 1, source="23andme", allele_1="A", allele_2="A")
        _seed_variant(conn, 2, ref="G", alt="G")
        _seed_call(conn, 2, 2, source="beagle_imputed", allele_1="G", allele_2="G")
        _seed_dbsnp_annotation(conn, 1, svid, ref="A", alts=("G",))

        result = canonicalize_variants(conn, no_backup=True)
        # Both old rows collapsed into one freshly-allocated survivor; query by
        # the canonical key rather than a specific variant_id.
        survivor = conn.execute(
            """
            SELECT has_genotyped_call, has_imputed_call
              FROM variants_master
             WHERE ref_allele = 'A' AND alt_allele = 'G'
            """,
        ).fetchone()
    assert result.rows_collapsed == 1
    assert result.new_variant_ids_allocated == 1
    assert survivor is not None
    assert survivor[0] is True  # has_genotyped_call (23andme)
    assert survivor[1] is True  # has_imputed_call (beagle_imputed absorbed)


def test_result_dataclass_is_frozen() -> None:
    """``CanonicalizeResult`` is frozen + slots so it can't be mutated."""
    result = CanonicalizeResult(
        dbsnp_source_version_id=1,
        already_canonical=True,
        rows_reoriented=0,
        rows_recovered_hom_ref=0,
        rows_recovered_hom_ref_multialt=0,
        rows_recovered_hom_alt=0,
        rows_collapsed=0,
        calls_repointed=0,
        new_variant_ids_allocated=0,
        survivors_flag_updated=0,
        genuine_variants_after=0,
        hom_ref_remaining=0,
        backup_path=None,
        wall_clock_seconds=0.0,
    )
    with pytest.raises(AttributeError):
        result.rows_reoriented = 5  # type: ignore[misc]
    assert result.rows_changed == 0
