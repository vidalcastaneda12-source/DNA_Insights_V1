"""``variant_aliases`` backfill — dbSNP rsID merge map for tier-2 matching.

The first of the post-5.7 backfills (ROADMAP "Post-5.7 backfills"). The dbSNP
loader (5.6, PR #59) populated ``dbsnp_annotations`` but left ``variant_aliases``
empty (finding-016 #8); this module fills it so the deferred tier-2 rsID merge
matching (finding-005 #4, a later PR) has a canonical ``old_rsid -> current_rsid``
map to resolve against.

**Why a separate file, not the dbSNP VCF.** The dbSNP VCF record exposes only
``record.ID`` (the *current* rsID) — it carries no list of the rsIDs that merged
*into* the record. Merge history lives in NCBI's legacy flat-file dump
``RsMergeArch.bcp.gz`` (the redesigned build-152+ pipeline embeds merges in the
per-chromosome RefSnp JSON, which is hundreds of GB and not viable for a
local-first app). RsMergeArch has been frozen since 2018 (build ~151), but
merges are append-only and historically monotonic, and the user's chip manifests
(23andMe v5, Ancestry v2) are contemporaneous with that horizon — so the frozen
file covers exactly the era of stale rsIDs a chip might carry. Post-2018 merges
are missed; that is a documented limitation, not a regression.

**Supersession.** ``variant_aliases`` is part of the **dbsnp source group**: it
shares the single ``annotation_sources`` pointer for ``'dbsnp'`` with
``dbsnp_annotations`` (PR #57 whitelisted both in
:data:`genome.annotate.supersession._SUPERSESSION_TABLES`). One ``dbsnp``
``source_version_id`` governs both tables. This backfill therefore writes alias
rows **under the source_version_id the dbsnp pointer already names** — it
allocates no new version and **does not flip the pointer** (the VCF stays put;
no 29 GB re-stream). The rows are "current" by construction because they share
the pointed-to id. After any future ``genome annotate refresh --source dbsnp``
flips the dbsnp pointer to a fresh id, this backfill must be re-run to re-attach
the map to the new epoch — the version-pointer pattern (finding-010) working as
designed.

It is **not** a registered loader: like :mod:`genome.annotate.index_refresh` it
is a standalone ``annotate`` subcommand (``refresh-aliases``), invoked via lazy
import from the CLI rather than routed through ``register_loader`` /
``refresh --source``. It lives beside ``dbsnp.py`` for cohesion with its
data-group sibling, but is deliberately absent from ``loaders/__init__.py``'s
eager side-effect imports (those are the registered loaders only).
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pyarrow as pa
import structlog

from genome.annotate.downloads import download_to_cache
from genome.annotate.source_versions import get_current_version
from genome.annotate.supersession import _next_alias_id, commit_and_checkpoint
from genome.db.duckdb_conn import duckdb_connection

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Upstream source (URL confirmed live 2026-05-27: 146 MB, modified 2018-02-07).
# ---------------------------------------------------------------------------

RSMERGEARCH_URL: Final[str] = (
    "https://ftp.ncbi.nih.gov/snp/organisms/human_9606/database/organism_data/RsMergeArch.bcp.gz"
)
"""NCBI dbSNP rs-merge archive (gzipped, tab-delimited BCP dump, ~146 MB)."""

URL_VERIFIED_DATE: Final[str] = "2026-05-27"

SOURCE_DB: Final[str] = "dbsnp"
"""Alias rows attach to the dbsnp source group's version pointer."""

_TARGET_TABLE: Final[str] = "variant_aliases"
_CACHE_FILENAME: Final[str] = "RsMergeArch.bcp.gz"
_RESOURCE_ID: Final[str] = "dbsnp_rsmergearch"

DEFAULT_BATCH_SIZE: Final[int] = 50_000
"""Bulk-insert chunk size; matches the dbSNP loader's amortised INSERT cost."""

_PROGRESS_EVERY: Final[int] = 5_000_000
"""Emit a scan-progress structlog line every N source rows (long-op contract)."""

# RsMergeArch.bcp column layout (0-indexed; no header; values are bare integers
# without an ``rs`` prefix). Confirmed against the dbSNP table schema:
#   0 rsHigh | 1 rsLow | 2 build_id | 3 orien | 4 create_time
#   5 last_updated_time | 6 rsCurrent | 7 orien2Current | 8 comment
# ``rsHigh`` is the merged-away rsID (-> alias_rsid); ``rsCurrent`` is the
# resolved survivor after transitive merges (-> current_rsid). ``rsLow`` is only
# the *immediate* merge target and is deliberately not used.
_COL_RSHIGH: Final[int] = 0
_COL_RSCURRENT: Final[int] = 6
_MIN_COLS: Final[int] = _COL_RSCURRENT + 1

_ALIAS_TYPE_MERGED: Final[str] = "merged"
"""Every RsMergeArch row is a merge. Withdrawals (SNPHistory.bcp.gz) and splits
are out of scope for this backfill (the schema's other alias_type values)."""

_RS_NUMERIC_RE: Final[re.Pattern[str]] = re.compile(r"^rs(\d+)$")

_ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("alias_id", pa.int64(), nullable=False),
        pa.field("alias_rsid", pa.string(), nullable=False),
        pa.field("current_rsid", pa.string(), nullable=False),
        pa.field("alias_type", pa.string()),
        pa.field("source_version_id", pa.int64(), nullable=False),
        pa.field("retrieval_date", pa.timestamp("us"), nullable=False),
    ],
)


# ---------------------------------------------------------------------------
# Errors + result.
# ---------------------------------------------------------------------------


class DbsnpNotLoadedError(RuntimeError):
    """Raised when no active dbSNP source-version exists to attach aliases to.

    ``variant_aliases`` shares the ``dbsnp`` version pointer, so the dbSNP VCF
    must be loaded first (``genome annotate refresh --source dbsnp``). Without a
    current pointer there is no ``source_version_id`` for the alias rows to live
    under, and writing them under a freshly-allocated id would orphan them from
    the pointer.
    """


@dataclass(frozen=True, slots=True)
class AliasRefreshResult:
    """Outcome of one :func:`refresh_aliases` call.

    Carries the drift identifiers the runbook compares across runs. The two
    ``user_*_hits`` counts are the proxies for how much tier-2 lift the later
    matching PR will land: ``user_old_rsid_hits`` is the count of user variants
    carrying a stale (merged-away) rsID that now has a canonical mapping.
    """

    target_source_version_id: int
    already_populated: bool
    source_rows_scanned: int
    rows_loaded: int
    distinct_alias_rsid: int
    distinct_current_rsid: int
    user_old_rsid_hits: int
    user_current_rsid_hits: int
    wall_clock_seconds: float


# ---------------------------------------------------------------------------
# User rsID set.
# ---------------------------------------------------------------------------


def _load_user_rsids(conn: DuckDBPyConnection) -> frozenset[int]:
    """Return the distinct numeric rsIDs present in ``variants_master``.

    Only ``rs<digits>`` values participate (23andMe ``i`` probe IDs and NULLs
    are ignored — RsMergeArch is entirely numeric). Stored as ``int`` so the
    per-row membership test against the ~80M-row merge file is a cheap set
    lookup rather than a string compare.
    """
    rows = conn.execute(
        "SELECT DISTINCT rsid FROM variants_master WHERE rsid LIKE 'rs%'",
    ).fetchall()
    user: set[int] = set()
    for (rsid,) in rows:
        if not isinstance(rsid, str):
            continue
        match = _RS_NUMERIC_RE.match(rsid)
        if match is not None:
            user.add(int(match.group(1)))
    return frozenset(user)


# ---------------------------------------------------------------------------
# Streaming parser.
# ---------------------------------------------------------------------------


@dataclass
class _ScanStats:
    """Mutable counters the generator updates so the caller can read totals.

    A generator yields only matched rows, so it cannot also return the source
    row total; this holder carries it back out once the generator is exhausted.
    """

    scanned: int = 0
    matched: int = 0


def _iter_merge_rows(
    path: Path,
    user_rsids: frozenset[int],
    source_version_id: int,
    retrieval_datetime: datetime,
    stats: _ScanStats,
) -> Iterator[dict[str, object]]:
    """Stream ``RsMergeArch.bcp.gz`` and yield user-relevant merge rows.

    A row is yielded when either ``rsHigh`` (the merged-away rsID) or
    ``rsCurrent`` (the survivor) appears in ``user_rsids`` — both directions are
    kept because tier-2 matching resolves a user's stale rsID *and* an external
    source's stale rsID against the user's current rsID. Self-merges
    (``rsHigh == rsCurrent``) and rows the user does not touch are dropped;
    output is deduped on ``alias_rsid`` (one survivor per merged-away rsID).
    Malformed / non-numeric rows are skipped defensively.

    ``stats`` is updated in place (``scanned`` per source row, ``matched`` per
    yield) so the caller has the totals after exhausting the generator. Emits a
    ``variant_aliases.scan.progress`` line every :data:`_PROGRESS_EVERY` source
    rows (the long-op progress contract).
    """
    retrieval_naive = retrieval_datetime.astimezone(UTC).replace(tzinfo=None)
    seen_alias: set[int] = set()
    with gzip.open(path, mode="rt", encoding="utf-8", errors="replace") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row in reader:
            stats.scanned += 1
            if stats.scanned % _PROGRESS_EVERY == 0:
                logger.info(
                    "variant_aliases.scan.progress",
                    rows_scanned=stats.scanned,
                    matched=stats.matched,
                )
            if len(row) < _MIN_COLS:
                continue
            try:
                rs_high = int(row[_COL_RSHIGH])
                rs_current = int(row[_COL_RSCURRENT])
            except (ValueError, IndexError):
                continue
            if rs_high == rs_current:
                continue
            if rs_high not in user_rsids and rs_current not in user_rsids:
                continue
            if rs_high in seen_alias:
                continue
            seen_alias.add(rs_high)
            stats.matched += 1
            yield {
                "alias_rsid": f"rs{rs_high}",
                "current_rsid": f"rs{rs_current}",
                "alias_type": _ALIAS_TYPE_MERGED,
                "source_version_id": source_version_id,
                "retrieval_date": retrieval_naive,
            }
    logger.info(
        "variant_aliases.scan.complete",
        rows_scanned=stats.scanned,
        matched=stats.matched,
    )


# ---------------------------------------------------------------------------
# Bulk insert (PyArrow Table registration + INSERT ... SELECT).
# ---------------------------------------------------------------------------


def _insert_batch(
    conn: DuckDBPyConnection,
    rows: list[dict[str, object]],
    *,
    base_id: int,
) -> int:
    """Bulk-insert one batch into ``variant_aliases`` via PyArrow + INSERT...SELECT.

    ``alias_id`` is allocated as ``range(base_id, base_id + n)``. Mirrors the
    dbSNP loader's ``_insert_batch`` (the project's locked bulk-load convention,
    CLAUDE.md — never ``executemany``).
    """
    if not rows:
        return 0
    n = len(rows)
    table_data: dict[str, pa.Array] = {
        "alias_id": pa.array(range(base_id, base_id + n), type=pa.int64()),
        "alias_rsid": pa.array([r["alias_rsid"] for r in rows], type=pa.string()),
        "current_rsid": pa.array([r["current_rsid"] for r in rows], type=pa.string()),
        "alias_type": pa.array([r["alias_type"] for r in rows], type=pa.string()),
        "source_version_id": pa.array(
            [r["source_version_id"] for r in rows],
            type=pa.int64(),
        ),
        "retrieval_date": pa.array(
            [r["retrieval_date"] for r in rows],
            type=pa.timestamp("us"),
        ),
    }
    table = pa.table(table_data, schema=_ARROW_SCHEMA)
    try:
        conn.register("_variant_aliases_stage_arrow", table)
        conn.execute(
            f"""
            INSERT INTO {_TARGET_TABLE} (
                alias_id, alias_rsid, current_rsid, alias_type,
                source_version_id, retrieval_date
            )
            SELECT
                alias_id, alias_rsid, current_rsid, alias_type,
                source_version_id, retrieval_date
              FROM _variant_aliases_stage_arrow
            """,  # noqa: S608 — table + column lists are module constants
        )
    finally:
        conn.unregister("_variant_aliases_stage_arrow")
    return n


def _count_rows_for_version(conn: DuckDBPyConnection, source_version_id: int) -> int:
    """``COUNT(*)`` of ``variant_aliases`` rows under ``source_version_id``."""
    row = conn.execute(
        f"SELECT COUNT(*) FROM {_TARGET_TABLE} WHERE source_version_id = ?",  # noqa: S608 — module constant
        [source_version_id],
    ).fetchone()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Post-load summary.
# ---------------------------------------------------------------------------


def _summarize(
    conn: DuckDBPyConnection,
    source_version_id: int,
) -> tuple[int, int, int, int, int]:
    """Compute the locked drift identifiers for ``source_version_id``.

    Returns ``(rows_loaded, distinct_alias_rsid, distinct_current_rsid,
    user_old_rsid_hits, user_current_rsid_hits)``.
    """
    counts_row = conn.execute(
        f"""
        SELECT
            COUNT(*),
            COUNT(DISTINCT alias_rsid),
            COUNT(DISTINCT current_rsid)
          FROM {_TARGET_TABLE}
         WHERE source_version_id = ?
        """,  # noqa: S608 — module constant
        [source_version_id],
    ).fetchone()
    rows_loaded = int(counts_row[0]) if counts_row is not None else 0
    distinct_alias = int(counts_row[1]) if counts_row is not None else 0
    distinct_current = int(counts_row[2]) if counts_row is not None else 0

    old_hits_row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT vm.rsid)
          FROM variants_master vm
          JOIN {_TARGET_TABLE} va
            ON va.alias_rsid = vm.rsid
           AND va.source_version_id = ?
        """,  # noqa: S608 — module constant
        [source_version_id],
    ).fetchone()
    user_old_hits = int(old_hits_row[0]) if old_hits_row is not None else 0

    current_hits_row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT vm.rsid)
          FROM variants_master vm
          JOIN {_TARGET_TABLE} va
            ON va.current_rsid = vm.rsid
           AND va.source_version_id = ?
        """,  # noqa: S608 — module constant
        [source_version_id],
    ).fetchone()
    user_current_hits = int(current_hits_row[0]) if current_hits_row is not None else 0

    return (
        rows_loaded,
        distinct_alias,
        distinct_current,
        user_old_hits,
        user_current_hits,
    )


def _build_result(
    conn: DuckDBPyConnection,
    *,
    source_version_id: int,
    already_populated: bool,
    source_rows_scanned: int,
    started: float,
) -> AliasRefreshResult:
    """Summarise the current alias set and pack an :class:`AliasRefreshResult`."""
    (
        rows_loaded,
        distinct_alias,
        distinct_current,
        user_old_hits,
        user_current_hits,
    ) = _summarize(conn, source_version_id)
    wall = time.monotonic() - started
    logger.info(
        "variant_aliases.refresh.complete",
        source_version_id=source_version_id,
        already_populated=already_populated,
        source_rows_scanned=source_rows_scanned,
        rows_loaded=rows_loaded,
        distinct_alias_rsid=distinct_alias,
        distinct_current_rsid=distinct_current,
        user_old_rsid_hits=user_old_hits,
        user_current_rsid_hits=user_current_hits,
        wall_clock_seconds=round(wall, 1),
    )
    return AliasRefreshResult(
        target_source_version_id=source_version_id,
        already_populated=already_populated,
        source_rows_scanned=source_rows_scanned,
        rows_loaded=rows_loaded,
        distinct_alias_rsid=distinct_alias,
        distinct_current_rsid=distinct_current,
        user_old_rsid_hits=user_old_hits,
        user_current_rsid_hits=user_current_hits,
        wall_clock_seconds=wall,
    )


# ---------------------------------------------------------------------------
# Top-level entrypoint.
# ---------------------------------------------------------------------------


def refresh_aliases(
    conn: DuckDBPyConnection | None = None,
    *,
    force: bool = False,
) -> AliasRefreshResult:
    """Populate ``variant_aliases`` from dbSNP's RsMergeArch under the dbsnp epoch.

    Pipeline:

    1. Resolve the current dbSNP ``source_version_id`` via the
       ``annotation_sources`` pointer. Raise :class:`DbsnpNotLoadedError` if
       none — the dbSNP VCF must be loaded first (fail fast, before any
       download).
    2. Re-run guard: if alias rows already exist under that id and ``force`` is
       not set, short-circuit (no re-download, no re-write) and return the
       existing summary.
    3. Download ``RsMergeArch.bcp.gz`` via the audited
       :func:`genome.annotate.downloads.download_to_cache` (gated on
       ``external_calls_enabled``; a disabled switch on an un-cached file emits
       the intent+blocked audit pair and raises ``ExternalCallsDisabledError``).
    4. In **one transaction**: ``DELETE`` the current-epoch rows when re-running
       with ``force`` (first population is a pure INSERT into the empty table),
       then stream-filter the merge file and chunk-insert the user-relevant rows
       via PyArrow under the same ``source_version_id``. The single-transaction
       wrap preserves supersession atomicity — a reader sees either the whole
       prior set or the whole new set (CLAUDE.md decision #7).

    The dbsnp pointer is **not** flipped and ``annotation_source_versions`` is
    **not** mutated (its ``record_count`` belongs to ``dbsnp_annotations``,
    which shares the row). ``conn`` defaults to a fresh read-write connection;
    a borrowed conn is left open for the caller (tests).
    """
    started = time.monotonic()

    ctx: contextlib.AbstractContextManager[DuckDBPyConnection] = (
        duckdb_connection() if conn is None else contextlib.nullcontext(conn)
    )
    with ctx as active_conn:
        current = get_current_version(active_conn, SOURCE_DB)
        if current is None:
            msg = (
                "no active dbSNP source-version; load the dbSNP VCF first via "
                "`genome annotate refresh --source dbsnp` before populating "
                "variant_aliases (they share the dbsnp version pointer)."
            )
            raise DbsnpNotLoadedError(msg)
        target_svid = current.source_version_id
        log = logger.bind(source_version_id=target_svid, force=force)

        existing = _count_rows_for_version(active_conn, target_svid)
        if existing > 0 and not force:
            log.info("variant_aliases.skip_already_populated", existing_rows=existing)
            return _build_result(
                active_conn,
                source_version_id=target_svid,
                already_populated=True,
                source_rows_scanned=0,
                started=started,
            )

        download = download_to_cache(
            SOURCE_DB,
            RSMERGEARCH_URL,
            _CACHE_FILENAME,
            resource_id=_RESOURCE_ID,
            force=force,
        )
        log.info(
            "variant_aliases.download_ready",
            path=str(download.path),
            size_bytes=download.size_bytes,
            sha256=download.sha256[:12],
        )

        user_rsids = _load_user_rsids(active_conn)
        log.info("variant_aliases.user_rsids_loaded", count=len(user_rsids))

        retrieval_datetime = datetime.now(UTC)
        active_conn.begin()
        try:
            if existing > 0:
                active_conn.execute(
                    f"DELETE FROM {_TARGET_TABLE} WHERE source_version_id = ?",  # noqa: S608 — module constant
                    [target_svid],
                )
                log.info("variant_aliases.cleared_for_reload", removed_rows=existing)

            base_id = _next_alias_id(active_conn)
            pending: list[dict[str, object]] = []
            inserted = 0
            stats = _ScanStats()

            def _flush() -> None:
                nonlocal pending, inserted, base_id
                if not pending:
                    return
                flushed = _insert_batch(active_conn, pending, base_id=base_id)
                inserted += flushed
                base_id += flushed
                pending = []

            for alias_row in _iter_merge_rows(
                download.path,
                user_rsids,
                target_svid,
                retrieval_datetime,
                stats,
            ):
                pending.append(alias_row)
                if len(pending) >= DEFAULT_BATCH_SIZE:
                    _flush()
            _flush()

            commit_and_checkpoint(active_conn, source_name=_TARGET_TABLE)
        except Exception:
            active_conn.rollback()
            raise

        log.info("variant_aliases.loaded", matched_rows=inserted)
        return _build_result(
            active_conn,
            source_version_id=target_svid,
            already_populated=False,
            source_rows_scanned=stats.scanned,
            started=started,
        )


__all__ = [
    "RSMERGEARCH_URL",
    "SOURCE_DB",
    "URL_VERIFIED_DATE",
    "AliasRefreshResult",
    "DbsnpNotLoadedError",
    "refresh_aliases",
]
