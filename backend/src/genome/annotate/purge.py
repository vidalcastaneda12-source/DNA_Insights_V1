"""General superseded-row purge for version-pointer annotation sources (PR 9).

Version-pointer supersession (CLAUDE.md decision #7, finding-010 #8) leaves the
prior version's rowset in each per-source table indefinitely under an older
``source_version_id`` (finding-010 #14). Live today: ``gnomad_frequencies`` holds
``source_version_id=8`` (4,467,370 superseded rows) alongside the active
``source_version_id=10`` (4,568,802 rows, ``annotation_sources`` pointer = 10).

This module ships the **general, runtime-derived** cleanup procedure. PR 7 was
closed moot — its hardcoded ``DELETE … IN (6,7,8,10)`` would erase the active
build on a rebuilt DB (finding-015). PR 9 instead re-derives, per supersedable
source, the ``(active, prior, deletable)`` partition from ``annotation_sources``
+ ``annotation_source_versions`` every invocation, reports the counts read-only,
and only under explicit ``execute=True`` deletes the deletable rows FK-safe.

Safety model — the active build must be **structurally undeletable** even by a
buggy predicate (the PR-7 trap, sharpened by finding-015: *active is the pointer
value, never newest-by-``ingested_at``*):

* **RAIL #1** — pointer-anchored, fail-closed partition (:func:`compute_purge_plan`,
  read-only). ``active_id`` is the ``annotation_sources`` pointer, authoritative.
* **RAIL #2** — a pre-flight ``active ∉ deletable`` assert *before* any DELETE.
* **belt** — an in-SQL ``AND source_version_id <> :active_id`` on the data DELETE,
  so the active set survives even a partition that wrongly lists it.
* **RAIL #6** — a post-delete negative control (pointer + active registry row +
  active row count unchanged), raising on any drift.
* **snapshot** — the pre-mutation ``genome.duckdb`` copy is the *sole hard
  recovery* for committed deletes (transactional rollback only covers
  within-transaction failure; once TX1 commits, the data deletes are durable).

FK-safety (finding-020 §3): DuckDB's FK-on-DELETE enforcement reads
*pre-transaction* state, so the data rows and the ``annotation_source_versions``
registry row are deleted in **two separate committed transactions** — TX1 data,
then TX2 registry — with a COUNT==0 guard over the **complete** FK-child set of
``annotation_source_versions`` between them. That set has **fourteen** members,
not the eight in :data:`~genome.annotate.supersession._SUPERSESSION_TABLES`, and
each must be counted on *its own* referencing column
(``annotation_sources.current_source_version_id``; the other 13
``source_version_id``) — a hardcoded ``WHERE source_version_id`` throws
``BinderException`` against ``annotation_sources`` *after* TX1 has already
committed. :func:`_fk_child_tables` derives the set + per-child column from
``duckdb_constraints()`` so the guard can never rot apart from the schema.

Scope: the seven sources with an ``annotation_sources`` pointer (``dbsnp`` covers
its two tables — ``dbsnp_annotations`` + ``variant_aliases`` — as one unit, obs
#5). The FK children with **no** pointer (``vep_consequences`` / ``genes`` /
``traits`` / ``pathways`` — ``genes`` sits under the ``hgnc`` ``svid=11`` seed,
obs #7) are never purge targets, but the guard still counts them by their real
column. Nothing here touches ``docs/schemas/`` or ``ddl/``.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from genome.annotate.canonicalize import take_snapshot
from genome.annotate.source_versions import KNOWN_SOURCE_DBS
from genome.annotate.supersession import _SUPERSESSION_TABLES
from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection

if TYPE_CHECKING:
    from pathlib import Path

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Static maps.
# ---------------------------------------------------------------------------


_SOURCE_DB_TABLES: Final[dict[str, tuple[str, ...]]] = {
    "clinvar": ("clinvar_annotations",),
    "gwas_catalog": ("gwas_catalog_associations",),
    "pharmgkb": ("pharmgkb_annotations",),
    "cpic": ("cpic_guidelines",),
    "gnomad": ("gnomad_frequencies",),
    "dbsnp": ("dbsnp_annotations", "variant_aliases"),
    "pgs_catalog": ("pgs_catalog_scores", "pgs_score_weights"),
}
"""``source_db`` → the data tables whose ``source_version_id`` rows it owns.

The deletion driver: TX1 deletes a superseded ``source_version_id``'s rows from
exactly these tables. ``dbsnp`` contributes two tables under one pointer (obs #5);
``pgs_catalog`` maps ``pgs_score_weights`` too — it is unpopulated today but
NOT-NULL-FKs the registry, so once Phase 6 populates it a superseded
``pgs_catalog`` registry DELETE would FK-fail unless its weights were cleared
first. Every name is a vetted module constant (S608-safe interpolation).
"""

_FK_CHILDREN_WITHOUT_POINTER: Final[frozenset[str]] = frozenset(
    {
        "vep_consequences",
        "genes",
        "traits",
        "pathways",
    },
)
"""FK children of ``annotation_source_versions`` with **no** ``annotation_sources``
pointer — never purge targets (``genes`` is populated under the ``hgnc``
``svid=11`` seed, obs #7). Recorded so the guard/drift test can reason about the
full child set; the runtime guard still counts these by their real column.
"""

_SNAPSHOT_SUBDIR: Final[str] = "purge"
_SNAPSHOT_LABEL: Final[str] = "purge-superseded"

_NO_POINTER: Final[int] = 0
"""``active_id`` sentinel for a source with no ``annotation_sources`` pointer row.

``source_version_id`` is allocated as ``MAX(...) + 1`` with a floor of 1, so 0 is
never a real id; a plan carrying ``active_id == _NO_POINTER`` is a fail-closed
keep-all (nothing deletable).
"""


# Import-time drift guards — SUBSET only, DB-free, cannot crash the CLI as the
# schema grows. The exact 14-child coverage equality lives in the test suite, not
# here (an ``==`` at import would break ``genome annotate`` the day a new FK child
# lands). These hold by construction over the constants above.
_ALL_MAPPED_TABLES: Final[frozenset[str]] = frozenset(
    table for tables in _SOURCE_DB_TABLES.values() for table in tables
)
assert _SUPERSESSION_TABLES <= _ALL_MAPPED_TABLES  # noqa: S101 — import-time drift guard
assert frozenset(_SOURCE_DB_TABLES) <= KNOWN_SOURCE_DBS  # noqa: S101 — import-time drift guard


# ---------------------------------------------------------------------------
# Result + plan shapes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcePurgePlan:
    """Read-only ``(active, prior, deletable)`` partition for one source."""

    source_db: str
    active_id: int
    prior_id: int | None
    deletable_ids: tuple[int, ...]
    tables: tuple[str, ...]
    row_counts: dict[int, int]


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Outcome of a :func:`purge_superseded` invocation (dry-run or execute)."""

    executed: bool
    plans: tuple[SourcePurgePlan, ...]
    data_rows_deleted: int
    registry_rows_deleted: int
    orphan_rows_swept: int
    backup_path: Path | None
    negative_control_ok: bool
    active_rows_unchanged: bool
    pointer_unchanged: bool


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class PurgeError(Exception):
    """Base class for all purge failures."""


class AmbiguousPartitionError(PurgeError):
    """The active build could not be uniquely identified (multiple pointer rows)."""


class DanglingPointerError(PurgeError):
    """``annotation_sources`` names a ``source_version_id`` with no registry row."""


class ActiveBuildAtRiskError(PurgeError):
    """The active ``source_version_id`` appeared in a deletable set (decision #7)."""


class RegistryStillReferencedError(PurgeError):
    """An FK child still references the registry row about to be deleted."""


class PurgeNegativeControlError(PurgeError):
    """The post-delete active-build invariant failed; restore from the snapshot."""


# ---------------------------------------------------------------------------
# Schema-derived FK-child catalog (single source for guard + drift test).
# ---------------------------------------------------------------------------


def _fk_child_tables(conn: DuckDBPyConnection) -> dict[str, str]:
    """Map ``{child_table: referencing_column}`` for every FK child of the registry.

    Derived from ``duckdb_constraints()`` (DuckDB 1.5.2) filtered to the foreign
    keys whose ``referenced_table`` is ``annotation_source_versions``. Returns all
    fourteen children: ``annotation_sources`` references via
    ``current_source_version_id``; the other thirteen via ``source_version_id``.
    This one helper feeds **both** the runtime registry-delete guard and the
    coverage drift test, so the two can never query different column sets.
    """
    rows = conn.execute(
        """
        SELECT table_name, constraint_column_names
          FROM duckdb_constraints()
         WHERE constraint_type = 'FOREIGN KEY'
           AND referenced_table = 'annotation_source_versions'
        """,
    ).fetchall()
    return {str(r[0]): str(r[1][0]) for r in rows}


def _first_fk_child_reference(
    conn: DuckDBPyConnection,
    fk_children: dict[str, str],
    svid: int,
) -> tuple[str, str, int] | None:
    """Return ``(table, column, count)`` of the first child still referencing ``svid``.

    ``None`` when no child references it. Each child is counted on **its own**
    referencing column (``annotation_sources`` → ``current_source_version_id``),
    so the guard never issues a ``WHERE source_version_id`` against a table that
    lacks that column (the BinderException headline crash).
    """
    for table, column in fk_children.items():
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",  # noqa: S608 — schema-catalog identifiers
            [svid],
        ).fetchone()
        if row is None:
            # COUNT(*) always returns a row; a None here means the catalog read itself failed.
            # Fail closed — never assume "0 references" on a destructive guard.
            msg = f"FK-child reference probe returned no row for {table}.{column} (svid {svid})"
            raise PurgeError(msg)
        count = int(row[0])
        if count > 0:
            return table, column, count
    return None


def _assert_no_fk_children(
    conn: DuckDBPyConnection,
    fk_children: dict[str, str],
    svid: int,
) -> None:
    """Fail-closed if any FK child still references ``svid`` before the registry DELETE."""
    hit = _first_fk_child_reference(conn, fk_children, svid)
    if hit is not None:
        table, column, count = hit
        msg = (
            f"refusing to delete annotation_source_versions row {svid}: "
            f"{count} row(s) in {table}.{column} still reference it"
        )
        raise RegistryStillReferencedError(msg)


# ---------------------------------------------------------------------------
# Read-only partition (RAIL #1).
# ---------------------------------------------------------------------------


def _data_row_counts(conn: DuckDBPyConnection, tables: tuple[str, ...]) -> dict[int, int]:
    """``{source_version_id: total rows}`` summed across ``tables`` (data-bearing only)."""
    counts: dict[int, int] = {}
    for table in tables:
        rows = conn.execute(
            f"SELECT source_version_id, COUNT(*) FROM {table} GROUP BY source_version_id",  # noqa: S608 — vetted _SOURCE_DB_TABLES identifier
        ).fetchall()
        for r in rows:
            svid = int(r[0])
            counts[svid] = counts.get(svid, 0) + int(r[1])
    return counts


def _ingested_at_map(conn: DuckDBPyConnection, source_db: str) -> dict[int, str]:
    """``{source_version_id: ingested_at (ISO VARCHAR)}`` for one source's registry rows."""
    rows = conn.execute(
        """
        SELECT source_version_id, CAST(ingested_at AS VARCHAR)
          FROM annotation_source_versions
         WHERE source_db = ?
        """,
        [source_db],
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def compute_purge_plan(
    conn: DuckDBPyConnection,
    source_db: str,
    keep: int = 1,
) -> SourcePurgePlan:
    """Re-derive the read-only ``(active, prior, deletable)`` partition for ``source_db``.

    ``active_id`` is the ``annotation_sources`` pointer value — authoritative,
    never recency/flag-derived (finding-015). ``keep`` retains that many *non-active*
    data-bearing versions (most-recent-first by ``ingested_at`` then ``svid``);
    ``keep=1`` keeps the single prior. The active build is excluded from
    ``deletable_ids`` by construction.

    Fail-closed: no ``annotation_sources`` row → keep-all
    (``active_id == _NO_POINTER``, ``deletable_ids == ()``); a pointer to a
    missing registry row → :class:`DanglingPointerError`; more than one pointer
    row → :class:`AmbiguousPartitionError`.
    """
    if source_db not in _SOURCE_DB_TABLES:
        msg = f"unknown purge source {source_db!r}; expected one of {sorted(_SOURCE_DB_TABLES)}"
        raise ValueError(msg)
    tables = _SOURCE_DB_TABLES[source_db]
    row_counts = _data_row_counts(conn, tables)

    pointer_rows = conn.execute(
        "SELECT current_source_version_id FROM annotation_sources WHERE source_db = ?",
        [source_db],
    ).fetchall()
    if len(pointer_rows) > 1:
        msg = (
            f"{source_db}: {len(pointer_rows)} annotation_sources pointer rows; "
            "expected exactly one"
        )
        raise AmbiguousPartitionError(msg)
    if not pointer_rows:
        return SourcePurgePlan(
            source_db=source_db,
            active_id=_NO_POINTER,
            prior_id=None,
            deletable_ids=(),
            tables=tables,
            row_counts=row_counts,
        )

    active_id = int(pointer_rows[0][0])
    registry_row = conn.execute(
        "SELECT 1 FROM annotation_source_versions WHERE source_version_id = ? AND source_db = ?",
        [active_id, source_db],
    ).fetchone()
    if registry_row is None:
        # The FK guarantees the id EXISTS, but it does NOT pin source_db — so a pointer to
        # another source's version is FK-valid yet semantically dangling, and catastrophic:
        # that id is absent from this source's data, so every real version (including the true
        # active build) would fall into ``deletable``. Fail closed (the active-build-undeletable
        # invariant; finding-015).
        msg = (
            f"{source_db}: annotation_sources pointer names source_version_id "
            f"{active_id}, which has no annotation_source_versions row for source_db "
            f"{source_db!r} (missing, or registered under a different source)"
        )
        raise DanglingPointerError(msg)

    ingested = _ingested_at_map(conn, source_db)
    non_active = sorted(
        (svid for svid in row_counts if svid != active_id),
        key=lambda s: (ingested.get(s, ""), s),
        reverse=True,
    )
    prior_id = non_active[0] if non_active else None
    kept_non_active = set(non_active[: max(keep, 0)])
    deletable_ids = tuple(sorted(set(non_active) - kept_non_active))
    return SourcePurgePlan(
        source_db=source_db,
        active_id=active_id,
        prior_id=prior_id,
        deletable_ids=deletable_ids,
        tables=tables,
        row_counts=row_counts,
    )


def _assert_active_not_deletable(plan: SourcePurgePlan) -> None:
    """RAIL #2 — the self-asserting decision-#7 invariant, run before any DELETE."""
    if plan.active_id in plan.deletable_ids:
        msg = (
            f"{plan.source_db}: active source_version_id {plan.active_id} is in the "
            f"deletable set {plan.deletable_ids}; refusing to proceed (decision #7)"
        )
        raise ActiveBuildAtRiskError(msg)


# ---------------------------------------------------------------------------
# Baseline + negative control (RAIL #3 + RAIL #6).
# ---------------------------------------------------------------------------


def _pointer_map(conn: DuckDBPyConnection) -> dict[str, int]:
    """``{source_db: current_source_version_id}`` over every ``annotation_sources`` row."""
    rows = conn.execute(
        "SELECT source_db, current_source_version_id FROM annotation_sources",
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _count_rows_for_svid(
    conn: DuckDBPyConnection,
    tables: tuple[str, ...],
    svid: int,
) -> int:
    """Total rows under ``svid`` across ``tables`` (active-build baseline / recount)."""
    total = 0
    for table in tables:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source_version_id = ?",  # noqa: S608 — vetted _SOURCE_DB_TABLES identifier
            [svid],
        ).fetchone()
        total += int(row[0]) if row is not None else 0
    return total


def _probe(
    conn: DuckDBPyConnection,
    in_scope: tuple[str, ...],
    keep: int,
    *,
    log: structlog.stdlib.BoundLogger,
) -> tuple[list[SourcePurgePlan], dict[str, int], dict[str, int]]:
    """Mandatory read-only probe — partition + RAIL #2 + protected baseline (RAIL #3)."""
    baseline_pointer = _pointer_map(conn)
    plans: list[SourcePurgePlan] = []
    baseline_active: dict[str, int] = {}
    for source_db in in_scope:
        plan = compute_purge_plan(conn, source_db, keep=keep)
        _assert_active_not_deletable(plan)
        plans.append(plan)
        baseline_active[source_db] = plan.row_counts.get(plan.active_id, 0)
        log.info(
            "purge.partition",
            source_db=source_db,
            active_id=plan.active_id,
            prior_id=plan.prior_id,
            deletable=list(plan.deletable_ids),
            deletable_rows=sum(plan.row_counts.get(s, 0) for s in plan.deletable_ids),
        )
    return plans, baseline_pointer, baseline_active


def _negative_control(
    conn: DuckDBPyConnection,
    plans: tuple[SourcePurgePlan, ...],
    baseline_pointer: dict[str, int],
    baseline_active: dict[str, int],
    *,
    backup_path: Path | None,
) -> tuple[bool, bool, bool]:
    """RAIL #6 — assert the active build survived; raise (naming the snapshot) on drift."""
    pointer_unchanged = _pointer_map(conn) == baseline_pointer
    active_rows_unchanged = True
    for plan in plans:
        if plan.active_id == _NO_POINTER:
            continue
        registry_row = conn.execute(
            "SELECT 1 FROM annotation_source_versions WHERE source_version_id = ?",
            [plan.active_id],
        ).fetchone()
        after = _count_rows_for_svid(conn, plan.tables, plan.active_id)
        if registry_row is None or after != baseline_active[plan.source_db]:
            active_rows_unchanged = False
    ok = pointer_unchanged and active_rows_unchanged
    if not ok:
        msg = (
            "purge negative control FAILED: the active build changed during the purge "
            f"(pointer_unchanged={pointer_unchanged}, active_rows_unchanged="
            f"{active_rows_unchanged}); restore genome.duckdb from {backup_path}"
        )
        raise PurgeNegativeControlError(msg)
    return ok, active_rows_unchanged, pointer_unchanged


# ---------------------------------------------------------------------------
# Mutation (TX1 data → guard → TX2 registry) + orphan self-heal.
# ---------------------------------------------------------------------------


def _delete_data_rows(
    conn: DuckDBPyConnection,
    plan: SourcePurgePlan,
    svid: int,
    *,
    log: structlog.stdlib.BoundLogger,
) -> int:
    """TX1 — delete ``svid``'s rows from the source's data tables, then commit+checkpoint.

    The ``AND source_version_id <> :active_id`` belt makes the active set
    structurally undeletable even if a buggy partition listed it.
    """
    conn.begin()
    try:
        deleted = 0
        for table in plan.tables:
            result = conn.execute(
                f"DELETE FROM {table} "  # noqa: S608 — vetted _SOURCE_DB_TABLES identifier
                "WHERE source_version_id = ? AND source_version_id <> ?",
                [svid, plan.active_id],
            )
            row = result.fetchone()
            rows = int(row[0]) if row is not None else 0
            deleted += rows
            log.info(
                "purge.table_deleted",
                source_db=plan.source_db,
                table=table,
                source_version_id=svid,
                rows=rows,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # CHECKPOINT runs only after a successful commit, OUTSIDE the rollback scope: a checkpoint
    # failure must not trigger a rollback of an already-committed transaction (mirrors
    # supersession.commit_and_checkpoint).
    conn.execute("CHECKPOINT")
    return deleted


def _delete_registry_row(
    conn: DuckDBPyConnection,
    svid: int,
    *,
    log: structlog.stdlib.BoundLogger,
) -> int:
    """TX2 — delete the ``annotation_source_versions`` row, then commit+checkpoint."""
    conn.begin()
    try:
        result = conn.execute(
            "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
            [svid],
        )
        row = result.fetchone()
        deleted = int(row[0]) if row is not None else 0
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    conn.execute("CHECKPOINT")  # post-commit, outside the rollback scope (see _delete_data_rows)
    log.info("purge.registry_deleted", source_version_id=svid, rows=deleted)
    return deleted


def _execute_deletions(
    conn: DuckDBPyConnection,
    plans: tuple[SourcePurgePlan, ...],
    fk_children: dict[str, str],
    *,
    log: structlog.stdlib.BoundLogger,
) -> tuple[int, int]:
    """Per deletable ``svid``: TX1 data → all-FK-children guard → TX2 registry."""
    data_deleted = 0
    registry_deleted = 0
    for plan in plans:
        for svid in plan.deletable_ids:
            log.info("purge.start", source_db=plan.source_db, source_version_id=svid)
            data_deleted += _delete_data_rows(conn, plan, svid, log=log)
            _assert_no_fk_children(conn, fk_children, svid)
            registry_deleted += _delete_registry_row(conn, svid, log=log)
    return data_deleted, registry_deleted


def _has_data_rows(conn: DuckDBPyConnection, tables: tuple[str, ...], svid: int) -> bool:
    """True if ``svid`` has at least one row in any of ``tables`` (short-circuit EXISTS).

    A ``LIMIT 1`` probe rather than a ``COUNT`` so a data-bearing svid (e.g. gnomad's
    4.47M-row prior) is dismissed on the first row instead of a full scan.
    """
    for table in tables:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE source_version_id = ? LIMIT 1",  # noqa: S608 — vetted _SOURCE_DB_TABLES identifier
            [svid],
        ).fetchone()
        if row is not None:
            return True
    return False


def _registry_orphans(
    conn: DuckDBPyConnection,
    plan: SourcePurgePlan,
    fk_children: dict[str, str],
) -> list[int]:
    """Non-active registry svids for the source with 0 data rows AND 0 FK-child references.

    A zero-data ``annotation_source_versions`` row for a pointer-bearing source — left by a
    refresh that allocated a version it never filled, or a crash between TX1 (data) and TX2
    (registry) — that nothing references. The plain partition's data-bearing filter skips it
    (it never appears in ``deletable_ids``), so it would accumulate forever. The active build
    is excluded by id, and pointer-less sources (``active_id == _NO_POINTER``) are left
    untouched (fail-closed). The fail-closed FK guard is re-checked before any DELETE.
    """
    if plan.active_id == _NO_POINTER:
        return []
    rows = conn.execute(
        "SELECT source_version_id FROM annotation_source_versions WHERE source_db = ?",
        [plan.source_db],
    ).fetchall()
    orphans: list[int] = []
    for r in rows:
        svid = int(r[0])
        if svid == plan.active_id:
            continue
        if _has_data_rows(conn, plan.tables, svid):
            continue
        if _first_fk_child_reference(conn, fk_children, svid) is not None:
            continue
        orphans.append(svid)
    return orphans


def _sweep_orphans(
    conn: DuckDBPyConnection,
    plans: tuple[SourcePurgePlan, ...],
    fk_children: dict[str, str],
    *,
    log: structlog.stdlib.BoundLogger,
) -> int:
    """Self-heal pass: delete every zero-data, unreferenced registry orphan in scope.

    Runs after the main deletion loop, re-derived live per source
    (:func:`_registry_orphans`), so it catches both a pre-existing zero-data registry row and
    any TX1-committed / TX2-skipped remnant of this run. The plain partition would NOT — its
    data-bearing filter skips a zero-row svid — so this sweep is the in-band recovery; the
    pre-mutation snapshot remains the sole hard backstop. Each registry DELETE is its own
    committed transaction behind the fail-closed FK guard.
    """
    swept = 0
    for plan in plans:
        for svid in _registry_orphans(conn, plan, fk_children):
            conn.begin()
            try:
                conn.execute(
                    "DELETE FROM annotation_source_versions WHERE source_version_id = ?",
                    [svid],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            conn.execute("CHECKPOINT")  # post-commit, outside the rollback scope
            swept += 1
            log.info("purge.orphan_swept", source_db=plan.source_db, source_version_id=svid)
    return swept


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def _resolve_sources(source: str | None) -> tuple[str, ...]:
    """All seven pointer-bearing sources, or the single ``--source`` narrowing."""
    if source is None:
        return tuple(_SOURCE_DB_TABLES)
    if source not in _SOURCE_DB_TABLES:
        msg = f"unknown purge source {source!r}; expected one of {sorted(_SOURCE_DB_TABLES)}"
        raise ValueError(msg)
    return (source,)


def purge_superseded(
    *,
    execute: bool = False,
    source: str | None = None,
    keep: int = 1,
    no_backup: bool = False,
    conn: DuckDBPyConnection | None = None,
) -> PurgeResult:
    """Purge superseded annotation rows, FK-safe, with the active build protected.

    Dry-run is the **default**: the mandatory read-only probe always runs first
    (RAIL #1 partition + RAIL #2 pre-flight assert + RAIL #3 baseline). ``execute``
    proceeds to mutate only when that probe surfaces real work — a non-empty deletable
    set OR a pre-existing zero-data registry orphan to self-heal; otherwise nothing is
    touched and no snapshot is taken (``keep=1`` against the current corpus, with no
    orphan, is a structural no-op — the single prior is protected).

    On ``execute`` with work: take a pre-mutation snapshot (unless ``no_backup``), then
    per deletable ``source_version_id`` run TX1 (data delete, active-belt) → the 14-child
    COUNT guard → TX2 (registry delete), sweep every zero-data unreferenced registry
    orphan in scope, and assert the post-delete negative control (RAIL #6).

    A borrowed ``conn`` (tests) reuses the connection and skips the snapshot by
    construction; an owned connection (``conn=None``) is **closed before** the
    snapshot so ``take_snapshot``'s own writer does not deadlock the single-writer
    lock, then reopened for the mutation (mirrors ``canonicalize``).
    """
    settings = get_settings()
    in_scope = _resolve_sources(source)
    log = logger.bind(execute=execute, keep=keep, source=source or "all")

    # ---- Mandatory read-only probe (always; dry-run is the default). ----
    probe_ctx: contextlib.AbstractContextManager[DuckDBPyConnection] = (
        duckdb_connection() if conn is None else contextlib.nullcontext(conn)
    )
    with probe_ctx as probe_conn:
        plans_list, baseline_pointer, baseline_active = _probe(probe_conn, in_scope, keep, log=log)
        probe_fk_children = _fk_child_tables(probe_conn)
        orphan_candidates = tuple(
            sorted(
                {
                    svid
                    for plan in plans_list
                    for svid in _registry_orphans(probe_conn, plan, probe_fk_children)
                },
            ),
        )

    plans = tuple(plans_list)
    targeted = tuple(sorted({svid for plan in plans for svid in plan.deletable_ids}))
    # Execute when there is ANY work: data-bearing deletable rows OR a pre-existing registry
    # orphan to self-heal (the latter is invisible to ``deletable_ids`` — finding-010 #14).
    work_ids = tuple(sorted(set(targeted) | set(orphan_candidates)))

    if not execute or not work_ids:
        log.info(
            "purge.complete",
            executed=False,
            deletable_total=len(targeted),
            orphan_candidates=len(orphan_candidates),
            data_rows_deleted=0,
            registry_rows_deleted=0,
        )
        return PurgeResult(
            executed=False,
            plans=plans,
            data_rows_deleted=0,
            registry_rows_deleted=0,
            orphan_rows_swept=0,
            backup_path=None,
            negative_control_ok=True,
            active_rows_unchanged=True,
            pointer_unchanged=True,
        )

    # ---- Snapshot (owned connection only; compute conn already closed above). ----
    backup_path: Path | None = None
    if conn is None and not no_backup:
        token = "svid" + "_".join(str(svid) for svid in work_ids)
        backup_path = take_snapshot(
            settings.genome_duckdb_path,
            archive_root=settings.archive_path,
            dbsnp_version=token,
            subdir=_SNAPSHOT_SUBDIR,
            label=_SNAPSHOT_LABEL,
        )

    # ---- Execute: TX1 data → guard → TX2 registry → orphan sweep → negative control. ----
    mutation_ctx: contextlib.AbstractContextManager[DuckDBPyConnection] = (
        duckdb_connection() if conn is None else contextlib.nullcontext(conn)
    )
    with mutation_ctx as active_conn:
        fk_children = _fk_child_tables(active_conn)
        data_rows_deleted, registry_rows_deleted = _execute_deletions(
            active_conn,
            plans,
            fk_children,
            log=log,
        )
        orphan_rows_swept = _sweep_orphans(active_conn, plans, fk_children, log=log)
        negative_control_ok, active_rows_unchanged, pointer_unchanged = _negative_control(
            active_conn,
            plans,
            baseline_pointer,
            baseline_active,
            backup_path=backup_path,
        )

    log.info(
        "purge.complete",
        executed=True,
        deletable_total=len(targeted),
        data_rows_deleted=data_rows_deleted,
        registry_rows_deleted=registry_rows_deleted,
        orphan_rows_swept=orphan_rows_swept,
        negative_control_ok=negative_control_ok,
        backup_path=str(backup_path) if backup_path is not None else None,
    )
    return PurgeResult(
        executed=True,
        plans=plans,
        data_rows_deleted=data_rows_deleted,
        registry_rows_deleted=registry_rows_deleted,
        orphan_rows_swept=orphan_rows_swept,
        backup_path=backup_path,
        negative_control_ok=negative_control_ok,
        active_rows_unchanged=active_rows_unchanged,
        pointer_unchanged=pointer_unchanged,
    )


__all__ = [
    "ActiveBuildAtRiskError",
    "AmbiguousPartitionError",
    "DanglingPointerError",
    "PurgeError",
    "PurgeNegativeControlError",
    "PurgeResult",
    "RegistryStillReferencedError",
    "SourcePurgePlan",
    "_fk_child_tables",
    "compute_purge_plan",
    "purge_superseded",
]
