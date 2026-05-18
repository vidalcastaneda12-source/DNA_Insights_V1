"""Generic ``is_active`` deactivation helper for evolving annotation sources.

The schema doc's "Soft-delete for evolving sources" principle (see
``docs/schemas/schema_group_2_reference_annotations.md``) marks every
ClinVar / GWAS Catalog / PharmGKB / CPIC / PGS Catalog row with
``is_active`` (and the four ClinVar-shaped tables additionally with
``superseded_by``). On refresh, the new ``source_version_id`` is
allocated first; then every prior row is flipped to
``is_active = FALSE`` (and pointed at the new version, where the column
exists). This module owns that flip in one parameterized statement,
gated on a fixed allow-list because the table name is interpolated into
the SQL.

``variant_annotations_index`` is *not* in the allow-list: it carries a
"current/superseded" feel too, but it is refreshed wholesale by job
(5.7), not by per-source supersession. Keeping it out of the list
prevents a future loader from accidentally rolling its rows alongside
its source-specific table.

The module also owns three observability helpers used by every loader:

* :func:`deactivate_prior_versions` emits ``supersession_update_start``
  and ``supersession_update_complete`` events around the UPDATE so the
  per-phase wall-clock is observable from the structlog stream alone.
* :func:`commit_and_checkpoint` wraps ``conn.commit()`` plus an
  explicit ``CHECKPOINT`` so the post-commit flush (which dominates
  large-table refreshes per finding-009) is measured inside the
  loader's wall-clock window instead of running opaquely after COMMIT
  returns.
* :func:`maybe_skip_same_version` returns a :class:`RefreshResult`
  short-circuit when the incoming refresh is provably a no-op against
  the currently-active version (finding-009 #14, ``--skip-if-same-version``
  CLI flag).
"""

from __future__ import annotations

import time
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
    },
)
"""Tables that carry ``is_active`` (and optionally ``superseded_by``).

Mirrors the "Soft-delete for evolving sources" set in
``docs/schemas/schema_group_2_reference_annotations.md``. ``table`` is
interpolated into the SQL string, so this whitelist is what makes that
interpolation safe.
"""


def deactivate_prior_versions(  # noqa: PLR0913 — all knobs keyword-only and named at every call site
    conn: DuckDBPyConnection,
    *,
    table: str,
    new_source_version_id: int,
    has_superseded_by: bool,
    source_name: str | None = None,
    force_all_active: bool = False,
) -> int:
    """Flip ``is_active = FALSE`` on rows ahead of a new active set.

    Two modes, gated by ``force_all_active``:

    * ``force_all_active=False`` (default, the normal new-version
      path). Deactivates only rows whose ``source_version_id`` is
      strictly less than ``new_source_version_id`` and ``is_active``
      is ``TRUE``. This is what every routine refresh against a fresh
      upstream release runs.
    * ``force_all_active=True`` (the ``--force`` re-run path).
      Deactivates every row whose ``is_active`` is ``TRUE``, with no
      ``source_version_id`` filter. ``upsert_source_version`` is
      idempotent on ``(source_db, version)`` and returns the existing
      id when the version label matches, so on a same-version
      ``--force`` re-run the prior active rows share the new
      ``source_version_id`` and the default predicate would skip them,
      leaving duplicate active rows after the bulk insert. The force
      mode sweeps them instead. Callers pass
      ``force_all_active=force`` so the supersession path is unified
      across normal and force refreshes — both go through this helper,
      both emit the same per-phase events, both honor
      ``has_superseded_by``.

    When ``has_superseded_by`` is ``True``, the deactivated rows are
    additionally tagged with ``superseded_by = new_source_version_id``
    so the supersession chain is followable. On a same-version
    ``--force`` re-run that means the prior rows point at the same
    ``source_version_id`` they were inserted under — the "self
    supersession" shape ClinVar's same-version refresh produces today
    (finding-009 #15 / #16).

    Returns the number of rows touched. Runs in one statement; the
    caller decides transaction boundaries (typically pairing this with
    :func:`upsert_source_version` and the subsequent per-source insert
    inside one ``conn.begin()`` / ``conn.commit()``).

    Emits ``supersession_update_start`` (with the pre-UPDATE
    ``prior_active_rows`` count) and ``supersession_update_complete``
    (with ``rows_deactivated`` and ``duration_ms``) so the UPDATE
    phase's wall-clock is measurable from the log stream alone. Per
    finding-009, the UPDATE on a 9M-row ClinVar refresh can take many
    minutes (~17-19 min on the real-data verification machine);
    emitting these events makes that window legible rather than
    silent. ``source_name`` is included in the event payload when
    provided so a multi-source refresh can be drilled into by source.
    Both events carry ``force_all_active`` so the log stream
    distinguishes the two modes — on a same-version ``--force`` re-run
    the prior count and deactivated count match the full active set,
    not just the strictly-older subset.

    Raises :class:`ValueError` when ``table`` is not in
    :data:`_SUPERSESSION_TABLES`.
    """
    if table not in _SUPERSESSION_TABLES:
        msg = (
            f"unknown supersession table {table!r}; expected one of {sorted(_SUPERSESSION_TABLES)}"
        )
        raise ValueError(msg)

    # The WHERE clause and matching params differ between modes; the
    # pre-UPDATE COUNT(*) uses the same predicate so ``prior_active_rows``
    # reflects what's actually about to be deactivated.
    if force_all_active:
        where_clause = "is_active = TRUE"
        count_params: list[object] = []
    else:
        where_clause = "source_version_id < ? AND is_active = TRUE"
        count_params = [new_source_version_id]

    prior_row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {where_clause}",  # noqa: S608 — table is whitelisted above
        count_params,
    ).fetchone()
    prior_active_rows = int(prior_row[0]) if prior_row is not None else 0

    logger.info(
        "supersession_update_start",
        source_name=source_name,
        table=table,
        new_source_version_id=new_source_version_id,
        prior_active_rows=prior_active_rows,
        force_all_active=force_all_active,
    )

    set_clause = "is_active = FALSE"
    update_params: list[object] = []
    if has_superseded_by:
        set_clause += ", superseded_by = ?"
        update_params.append(new_source_version_id)
    update_params.extend(count_params)

    sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"  # noqa: S608 — table is whitelisted above
    started = time.monotonic()
    row = conn.execute(sql, update_params).fetchone()
    duration_ms = int((time.monotonic() - started) * 1000)
    # DuckDB returns the number of changed rows as a one-tuple on the
    # UPDATE result. Treat a missing/empty row defensively as zero so a
    # zero-row deactivation still has a sensible return value.
    touched = int(row[0]) if row is not None else 0

    logger.info(
        "supersession_update_complete",
        source_name=source_name,
        table=table,
        new_source_version_id=new_source_version_id,
        rows_deactivated=touched,
        duration_ms=duration_ms,
        force_all_active=force_all_active,
    )
    return touched


def commit_and_checkpoint(
    conn: DuckDBPyConnection,
    *,
    source_name: str,
) -> None:
    """COMMIT the open transaction, then run an explicit ``CHECKPOINT``.

    DuckDB schedules a checkpoint automatically when the WAL crosses
    ``checkpoint_threshold`` (default 16 MB), but the timing is opaque
    from the loader's perspective: on a large supersession transaction
    the post-COMMIT flush can take many minutes (finding-009 measured
    ~23 minutes on a 9M-row ClinVar refresh) and the loader has no log
    output across that window. This helper makes the flush observable:

    * ``supersession_commit_start`` / ``supersession_commit_complete``
      bracket the DuckDB ``COMMIT`` itself.
    * ``supersession_checkpoint_start`` /
      ``supersession_checkpoint_complete`` bracket the explicit
      ``CHECKPOINT`` issued immediately after.

    Both phases report ``duration_ms`` in the ``_complete`` event so a
    reader can attribute time spent without correlating timestamps
    across events. Total wall-clock is unchanged -- this is a
    measurement step, not an algorithmic change (finding-009 #11).
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
    ``annotation_source_versions`` for the currently-active row for
    ``source_db``. If that row exists *and* its ``version`` matches the
    incoming ``version`` *and* its ``source_file_hash`` matches the
    incoming ``source_file_hash``, the refresh would write no new
    information: emit a ``supersession_skipped_same_version`` event
    naming the matched ``source_version_id`` and return a
    :class:`RefreshResult` with ``was_already_current=True``.

    Returns ``None`` when:

    * ``skip_if_same_version`` is ``False`` (the flag is opt-in; the
      caller must proceed with normal supersession);
    * no current active row exists for ``source_db``;
    * the current active row's ``version`` differs from the incoming
      ``version`` (a true new release);
    * the current active row's ``source_file_hash`` differs from the
      incoming ``source_file_hash`` (the upstream silently re-generated
      the file under the same version label -- safer to re-load).

    The match is on the *currently-active* row: a matching but
    superseded older row does *not* short-circuit, because the loader's
    job is to land a fresh active set.
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
    "commit_and_checkpoint",
    "deactivate_prior_versions",
    "maybe_skip_same_version",
]
