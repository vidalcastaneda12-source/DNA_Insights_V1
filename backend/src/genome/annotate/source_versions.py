"""``annotation_source_versions`` upsert + read helpers.

The Phase 5 loader scaffold's persistent-state layer. Every per-source
loader (5.1+) writes one row here per refresh: version label, source URL,
file hash, file size, and record count. The application invariant
documented in
``docs/schemas/schema_group_2_reference_annotations.md`` (one
``is_current = TRUE`` row per ``source_db``) is enforced here.

This module owns:

* :data:`KNOWN_SOURCE_DBS` — the canonical set of ``source_db`` labels
  taken from the schema doc's ``CREATE TABLE`` comment. Pulled out as a
  frozenset so a typo in a future 5.1+ loader is caught immediately
  without requiring a DDL change.
* :class:`SourceVersion` — typed read shape for one row.
* :func:`upsert_source_version` — idempotent on ``(source_db, version)``;
  deactivates the prior ``is_current = TRUE`` row inside the same
  transaction as the insert.
* :func:`get_current_version` — quick "what's loaded?" lookup.

App-allocated BIGINT primary keys mirror
:func:`genome.imputation.runs._next_imputation_id`: ``MAX(...) + 1`` from
the table, no DuckDB sequence.

DuckDB FK + index quirk
-----------------------

The schema's ``idx_asv_current`` index covers ``(source_db, is_current)``.
DuckDB rewrites an UPDATE on an indexed column as DELETE+INSERT
internally, which fails its FK constraint check when child rows in
``clinvar_annotations`` / ``gwas_catalog_associations`` / etc. still
reference the row being touched — even though the PK is not actually
changing. The upsert below works around this by dropping the index,
running the UPDATE + INSERT inside one transaction, then re-creating
the index. The DDL operations are deliberately outside the transaction
because dropping an index inside a transaction in DuckDB does not make
its absence visible to the same transaction's subsequent UPDATE. This
keeps the schema unchanged (no DDL file edits) while still respecting
the schema doc's "deactivate then insert in one transaction"
invariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

KNOWN_SOURCE_DBS: Final[frozenset[str]] = frozenset(
    {
        "clinvar",
        "gwas_catalog",
        "pharmgkb",
        "cpic",
        "pgs_catalog",
        "gnomad",
        "dbsnp",
        "vep",
        "hgnc",
        "efo",
        "kegg",
    },
)
"""Canonical ``source_db`` labels.

Mirrors the ``CREATE TABLE`` comment in
``docs/schemas/schema_group_2_reference_annotations.md``. A future loader
that needs a new label must add it here in the same PR.
"""


@dataclass(frozen=True, slots=True)
class SourceVersion:
    """One row in ``annotation_source_versions``."""

    source_version_id: int
    source_db: str
    version: str
    ingested_at: str
    source_url: str | None
    source_file_hash: str | None
    source_file_size: int | None
    record_count: int | None
    is_current: bool
    notes: str | None


def _next_source_version_id(conn: DuckDBPyConnection) -> int:
    """``MAX(source_version_id) + 1``; mirrors ``runs._next_imputation_id``."""
    row = conn.execute(
        "SELECT COALESCE(MAX(source_version_id), 0) FROM annotation_source_versions",
    ).fetchone()
    return int(row[0]) + 1 if row is not None else 1


def _row_to_dataclass(row: tuple[object, ...]) -> SourceVersion:
    (
        source_version_id,
        source_db,
        version,
        ingested_at,
        source_url,
        source_file_hash,
        source_file_size,
        record_count,
        is_current,
        notes,
    ) = row
    return SourceVersion(
        source_version_id=int(source_version_id),  # type: ignore[call-overload]
        source_db=str(source_db),
        version=str(version),
        ingested_at=str(ingested_at),
        source_url=None if source_url is None else str(source_url),
        source_file_hash=None if source_file_hash is None else str(source_file_hash),
        source_file_size=None if source_file_size is None else int(source_file_size),  # type: ignore[call-overload]
        record_count=None if record_count is None else int(record_count),  # type: ignore[call-overload]
        is_current=bool(is_current),
        notes=None if notes is None else str(notes),
    )


def upsert_source_version(  # noqa: PLR0913 — schema fields are not collapsible
    conn: DuckDBPyConnection,
    *,
    source_db: str,
    version: str,
    source_url: str | None,
    source_file_hash: str,
    source_file_size: int,
    record_count: int | None,
    notes: str | None = None,
) -> int:
    """Insert a new ``annotation_source_versions`` row.

    Side effects:

    1. Validate ``source_db`` against :data:`KNOWN_SOURCE_DBS` first.
    2. If ``(source_db, version)`` already exists, return the existing
       ``source_version_id`` and do not write — this makes
       ``genome annotate refresh`` safe to re-run on the same on-disk
       snapshot.
    3. Otherwise, inside one transaction: deactivate the prior
       ``is_current = TRUE`` row for ``source_db`` (if any) and insert
       the new row with ``is_current = TRUE``.
    """
    if source_db not in KNOWN_SOURCE_DBS:
        msg = f"unknown source_db {source_db!r}; expected one of {sorted(KNOWN_SOURCE_DBS)}"
        raise ValueError(msg)

    existing = conn.execute(
        """
        SELECT source_version_id
          FROM annotation_source_versions
         WHERE source_db = ? AND version = ?
        """,
        [source_db, version],
    ).fetchone()
    if existing is not None:
        logger.debug(
            "annotate.source_version.exists",
            source_db=source_db,
            version=version,
            source_version_id=int(existing[0]),
        )
        return int(existing[0])

    new_id = _next_source_version_id(conn)
    # DuckDB FK+index quirk: drop ``idx_asv_current`` so the UPDATE
    # below isn't rewritten as DELETE+INSERT against a row still
    # referenced by child tables. The index is re-created after the
    # transaction commits. See the module docstring for context.
    conn.execute("DROP INDEX IF EXISTS idx_asv_current")
    index_dropped = True
    try:
        conn.begin()
        try:
            conn.execute(
                """
                UPDATE annotation_source_versions
                   SET is_current = FALSE
                 WHERE source_db = ? AND is_current = TRUE
                """,
                [source_db],
            )
            conn.execute(
                """
                INSERT INTO annotation_source_versions (
                    source_version_id, source_db, version, source_url,
                    source_file_hash, source_file_size, record_count,
                    is_current, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, TRUE, ?)
                """,
                [
                    new_id,
                    source_db,
                    version,
                    source_url,
                    source_file_hash,
                    source_file_size,
                    record_count,
                    notes,
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        if index_dropped:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_asv_current "
                "ON annotation_source_versions(source_db, is_current)",
            )
    logger.info(
        "annotate.source_version.inserted",
        source_db=source_db,
        version=version,
        source_version_id=new_id,
    )
    return new_id


def get_current_version(
    conn: DuckDBPyConnection,
    source_db: str,
) -> SourceVersion | None:
    """Return the ``is_current = TRUE`` row for ``source_db`` or ``None``."""
    row = conn.execute(
        """
        SELECT
            source_version_id, source_db, version,
            CAST(ingested_at AS VARCHAR),
            source_url, source_file_hash, source_file_size,
            record_count, is_current, notes
          FROM annotation_source_versions
         WHERE source_db = ? AND is_current = TRUE
        """,
        [source_db],
    ).fetchone()
    if row is None:
        return None
    return _row_to_dataclass(row)


__all__ = [
    "KNOWN_SOURCE_DBS",
    "SourceVersion",
    "get_current_version",
    "upsert_source_version",
]
