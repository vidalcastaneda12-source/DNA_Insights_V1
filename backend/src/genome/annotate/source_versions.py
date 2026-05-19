"""``annotation_source_versions`` insert + read helpers.

The Phase 5 loader scaffold's persistent-state layer. Every per-source
loader writes one row here per refresh: version label, source URL,
file hash, file size, and record count. The row is the supersession
audit trail; the "currently active" version is named separately by the
single-row pointer in ``annotation_sources`` (see
:mod:`genome.annotate.supersession`).

This module owns:

* :data:`KNOWN_SOURCE_DBS` — the canonical set of ``source_db`` labels
  taken from the schema doc's ``CREATE TABLE`` comment. Pulled out as a
  frozenset so a typo in a future loader is caught immediately
  without requiring a DDL change.
* :class:`SourceVersion` — typed read shape for one row.
* :func:`insert_source_version` — always allocates a new row. Multiple
  rows may share the same ``(source_db, version)``; identity is the
  ``source_version_id`` PK alone. The version-pointer flip in
  ``annotation_sources`` is what makes one of them "current".
* :func:`get_current_version` — quick "what's loaded?" lookup. Reads
  via the ``annotation_sources`` pointer rather than a per-row flag.

App-allocated BIGINT primary keys mirror
:func:`genome.imputation.runs._next_imputation_id`: ``MAX(...) + 1`` from
the table, no DuckDB sequence.
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
        notes=None if notes is None else str(notes),
    )


def insert_source_version(  # noqa: PLR0913 — schema fields are not collapsible
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

    Every call allocates a brand-new ``source_version_id``. Multiple
    rows may share the same ``(source_db, version)`` -- e.g. when a
    user re-runs a loader with ``--force`` against an unchanged
    upstream release, the audit trail still gets one row per refresh.
    The version-pointer flip in ``annotation_sources`` (separate
    transaction, separate call) is what designates one of those rows
    as "current".

    The pre-PR per-row supersession model performed an idempotent
    ``(source_db, version)``-keyed upsert here and deactivated the
    prior ``is_current = TRUE`` row inside the same transaction. Under
    the version-pointer model the idempotence belongs upstream: the
    loader's own pre-refresh check (``get_current_version`` +
    ``--skip-if-same-version``) decides whether to call this function
    at all. Once called, the function always writes.

    Validates ``source_db`` against :data:`KNOWN_SOURCE_DBS`.
    """
    if source_db not in KNOWN_SOURCE_DBS:
        msg = f"unknown source_db {source_db!r}; expected one of {sorted(KNOWN_SOURCE_DBS)}"
        raise ValueError(msg)

    new_id = _next_source_version_id(conn)
    conn.execute(
        """
        INSERT INTO annotation_source_versions (
            source_version_id, source_db, version, source_url,
            source_file_hash, source_file_size, record_count, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    """Return the currently-active version row for ``source_db`` or ``None``.

    "Currently active" means the row named by ``annotation_sources``
    for this source. Returns ``None`` when no pointer row exists yet
    (first-load) or when the FK target has been deleted out from
    under the pointer (should not happen under normal operation).
    """
    row = conn.execute(
        """
        SELECT
            asv.source_version_id, asv.source_db, asv.version,
            CAST(asv.ingested_at AS VARCHAR),
            asv.source_url, asv.source_file_hash, asv.source_file_size,
            asv.record_count, asv.notes
          FROM annotation_sources a
          JOIN annotation_source_versions asv
            ON asv.source_version_id = a.current_source_version_id
         WHERE a.source_db = ?
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
    "insert_source_version",
]
