"""Version-pointer supersession for evolving annotation sources.

The schema doc's "Soft-delete for evolving sources" principle is now
implemented by a single-row pointer in :data:`annotation_sources` rather
than by per-row ``is_active`` flips on the evolving annotation tables
themselves. Each ClinVar / GWAS Catalog / PharmGKB / CPIC / PGS Catalog
source has one row in ``annotation_sources`` whose
``current_source_version_id`` names the version that "is current" right
now. A refresh becomes:

1. INSERT the new active set under a fresh ``source_version_id`` (via the
   per-source loader's chunked bulk insert).
2. UPSERT ``annotation_sources`` for this source so its pointer flips to
   the new ``source_version_id``.

That UPSERT is one statement against one row. Supersession atomicity
(CLAUDE.md decision #7) is preserved by construction — there is no mass
UPDATE to wrap in a transaction; the pointer flip is the supersession
event. ClinVar's 19-minute UPDATE phase (finding-009 #15) disappears.

Readers that want "the current version's rows" join through
``annotation_sources`` instead of filtering on a per-row ``is_active``
column. The prior rows stay in the per-source table indefinitely, keyed
by the older ``source_version_id``; readers that want the prior set
filter on that id directly. The supersession chain lives in
``annotation_source_versions`` (the version registry's ``ingested_at``
gives the ordering; the current pointer in ``annotation_sources``
names the active row).

This module owns three observability helpers used by every loader:

* :func:`flip_to_new_version` flips the per-source pointer in
  ``annotation_sources`` and emits a ``supersession_version_flip`` event
  with prior + new version ids and row counts. Returns a
  :class:`VersionFlipResult` carrying the same payload so callers can
  log it without re-querying.
* :func:`commit_and_checkpoint` wraps ``conn.commit()`` plus an explicit
  ``CHECKPOINT`` so the post-commit flush after the bulk INSERT is
  measured inside the loader's wall-clock window instead of running
  opaquely after COMMIT returns.
* :func:`maybe_skip_same_version` returns a :class:`RefreshResult`
  short-circuit when the incoming refresh is provably a no-op against
  the currently-active version (finding-009 #14, ``--skip-if-same-version``
  CLI flag).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from genome.annotate.registry import RefreshResult
from genome.annotate.source_versions import get_current_version
from genome.db.duckdb_conn import duckdb_connection

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

_SUPERSESSION_TABLES: Final[frozenset[str]] = frozenset(
    {
        "clinvar_annotations",
        "gwas_catalog_associations",
        "pharmgkb_annotations",
        "cpic_guidelines",
        "pgs_catalog_scores",
        "gnomad_frequencies",
    },
)
"""Per-source tables whose "current" rows are identified via the version pointer.

Mirrors the "Soft-delete for evolving sources" set in
``docs/schemas/schema_group_2_reference_annotations.md``. ``table`` is
interpolated into the COUNT(*) SQL :func:`flip_to_new_version` issues for
the event payload, so this whitelist is what makes that interpolation
safe.
"""


@dataclass(frozen=True, slots=True)
class VersionFlipResult:
    """Outcome of one :func:`flip_to_new_version` call.

    Carries the exact payload of the ``supersession_version_flip`` event
    so callers can log the same fields without re-querying.
    """

    source: str
    prior_version_id: int | None
    new_version_id: int
    prior_row_count: int
    new_row_count: int
    elapsed_ms: int


def flip_to_new_version(
    conn: DuckDBPyConnection,
    *,
    source: str,
    table: str,
    new_source_version_id: int,
) -> VersionFlipResult:
    """Flip ``annotation_sources`` for ``source`` to point at ``new_source_version_id``.

    Single-row UPSERT against ``annotation_sources``: either INSERT the
    first-load row for this source or UPDATE the existing pointer to the
    new version id. The pointer flip IS the supersession event; the prior
    set is no longer "current" the moment this statement commits.

    ``table`` names the per-source table the row counts come from. The
    counts are computed for the event payload:

    * ``prior_row_count`` — rows whose ``source_version_id`` matches the
      pointer's prior value (NULL on first load → count is 0).
    * ``new_row_count`` — rows whose ``source_version_id`` matches the
      new value. Called after the loader's chunked INSERT so the new
      set is already in the table.

    Emits ``supersession_version_flip`` with ``source``,
    ``prior_version_id``, ``new_version_id``, ``prior_row_count``,
    ``new_row_count``, and ``elapsed_ms``. Returns the same payload as a
    :class:`VersionFlipResult` so callers can log it without re-querying.

    Raises :class:`ValueError` when ``table`` is not in
    :data:`_SUPERSESSION_TABLES`.
    """
    if table not in _SUPERSESSION_TABLES:
        msg = (
            f"unknown supersession table {table!r}; expected one of {sorted(_SUPERSESSION_TABLES)}"
        )
        raise ValueError(msg)

    started = time.monotonic()

    prior_row = conn.execute(
        "SELECT current_source_version_id FROM annotation_sources WHERE source_db = ?",
        [source],
    ).fetchone()
    prior_version_id: int | None = int(prior_row[0]) if prior_row is not None else None

    if prior_version_id is None:
        prior_row_count = 0
    else:
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source_version_id = ?",  # noqa: S608 — table is whitelisted above
            [prior_version_id],
        ).fetchone()
        prior_row_count = int(count_row[0]) if count_row is not None else 0

    # Single-row pointer UPSERT — INSERT the first-load row or UPDATE the
    # existing pointer. DuckDB's ON CONFLICT DO UPDATE handles both modes
    # in one statement.
    conn.execute(
        """
        INSERT INTO annotation_sources (source_db, current_source_version_id)
             VALUES (?, ?)
        ON CONFLICT (source_db) DO UPDATE
                SET current_source_version_id = excluded.current_source_version_id
        """,
        [source, new_source_version_id],
    )

    new_count_row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE source_version_id = ?",  # noqa: S608 — table is whitelisted above
        [new_source_version_id],
    ).fetchone()
    new_row_count = int(new_count_row[0]) if new_count_row is not None else 0

    elapsed_ms = int((time.monotonic() - started) * 1000)

    logger.info(
        "supersession_version_flip",
        source=source,
        prior_version_id=prior_version_id,
        new_version_id=new_source_version_id,
        prior_row_count=prior_row_count,
        new_row_count=new_row_count,
        elapsed_ms=elapsed_ms,
    )

    return VersionFlipResult(
        source=source,
        prior_version_id=prior_version_id,
        new_version_id=new_source_version_id,
        prior_row_count=prior_row_count,
        new_row_count=new_row_count,
        elapsed_ms=elapsed_ms,
    )


def commit_and_checkpoint(
    conn: DuckDBPyConnection,
    *,
    source_name: str,
) -> None:
    """COMMIT the open transaction, then run an explicit ``CHECKPOINT``.

    DuckDB schedules a checkpoint automatically when the WAL crosses
    ``checkpoint_threshold`` (default 16 MB), but the timing is opaque
    from the loader's perspective. The COMMIT itself flushes the dirty
    pages synchronously; the explicit CHECKPOINT that follows is
    effectively a no-op on the supersession path (finding-009 #15) but
    the event still fires so the measurement is durable.

    ``supersession_commit_start`` / ``supersession_commit_complete``
    bracket the DuckDB COMMIT, and
    ``supersession_checkpoint_start`` /
    ``supersession_checkpoint_complete`` bracket the explicit CHECKPOINT.
    Both phases report ``duration_ms`` in the ``_complete`` event so a
    reader can attribute time spent without correlating timestamps across
    events.
    """
    commit_started = time.monotonic()
    logger.info("supersession_commit_start", source_name=source_name)
    conn.commit()
    commit_ms = int((time.monotonic() - commit_started) * 1000)
    logger.info(
        "supersession_commit_complete",
        source_name=source_name,
        duration_ms=commit_ms,
    )

    checkpoint_started = time.monotonic()
    logger.info("supersession_checkpoint_start", source_name=source_name)
    conn.execute("CHECKPOINT")
    checkpoint_ms = int((time.monotonic() - checkpoint_started) * 1000)
    logger.info(
        "supersession_checkpoint_complete",
        source_name=source_name,
        duration_ms=checkpoint_ms,
    )


def maybe_skip_same_version(
    *,
    source_db: str,
    version: str,
    source_file_hash: str,
    skip_if_same_version: bool,
) -> RefreshResult | None:
    """Return a :class:`RefreshResult` short-circuit if the refresh is a no-op.

    Implements the ``--skip-if-same-version`` CLI flag (finding-009 #14).
    When ``skip_if_same_version`` is ``True``, queries
    ``annotation_sources`` for the currently-active version for
    ``source_db`` (and the matching ``annotation_source_versions`` row).
    If that version's ``version`` matches the incoming ``version`` *and*
    its ``source_file_hash`` matches the incoming ``source_file_hash``,
    the refresh would write no new information: emit a
    ``supersession_skipped_same_version`` event naming the matched
    ``source_version_id`` and return a :class:`RefreshResult` with
    ``was_already_current=True``.

    Returns ``None`` when:

    * ``skip_if_same_version`` is ``False`` (the flag is opt-in; the
      caller must proceed with normal supersession);
    * no current pointer row exists for ``source_db`` (first load);
    * the current version's ``version`` differs from the incoming
      ``version`` (a true new release);
    * the current version's ``source_file_hash`` differs from the
      incoming ``source_file_hash`` (the upstream silently re-generated
      the file under the same version label -- safer to re-load).

    The match is on the *currently-active* version via the version
    pointer: a matching but superseded older version does *not*
    short-circuit, because the loader's job is to land a fresh active set.
    """
    if not skip_if_same_version:
        return None

    with duckdb_connection(read_only=True) as conn:
        current = get_current_version(conn, source_db)

    if current is None:
        return None
    if current.version != version:
        return None
    if current.source_file_hash != source_file_hash:
        return None

    logger.info(
        "supersession_skipped_same_version",
        source_db=source_db,
        source_version_id=current.source_version_id,
        version=version,
    )
    return RefreshResult(
        source_db=source_db,
        source_version_id=current.source_version_id,
        version=version,
        record_count=current.record_count or 0,
        was_already_current=True,
    )


__all__ = [
    "VersionFlipResult",
    "commit_and_checkpoint",
    "flip_to_new_version",
    "maybe_skip_same_version",
]
