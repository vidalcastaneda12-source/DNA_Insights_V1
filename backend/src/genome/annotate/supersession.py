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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

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


def deactivate_prior_versions(
    conn: DuckDBPyConnection,
    *,
    table: str,
    new_source_version_id: int,
    has_superseded_by: bool,
) -> int:
    """Flip ``is_active = FALSE`` on every row older than ``new_source_version_id``.

    When ``has_superseded_by`` is ``True``, the deactivated rows are
    additionally tagged with ``superseded_by = new_source_version_id``
    so the supersession chain is followable.

    Returns the number of rows touched. Runs in one statement; the
    caller decides transaction boundaries (typically pairing this with
    :func:`upsert_source_version` and the subsequent per-source insert
    inside one ``conn.begin()`` / ``conn.commit()``).

    Raises :class:`ValueError` when ``table`` is not in
    :data:`_SUPERSESSION_TABLES`.
    """
    if table not in _SUPERSESSION_TABLES:
        msg = (
            f"unknown supersession table {table!r}; expected one of {sorted(_SUPERSESSION_TABLES)}"
        )
        raise ValueError(msg)

    set_clause = "is_active = FALSE"
    if has_superseded_by:
        set_clause += ", superseded_by = ?"
        params: list[object] = [new_source_version_id, new_source_version_id]
    else:
        params = [new_source_version_id]

    sql = (
        f"UPDATE {table} SET {set_clause} "  # noqa: S608 — table is whitelisted above
        "WHERE source_version_id < ? AND is_active = TRUE"
    )
    row = conn.execute(sql, params).fetchone()
    # DuckDB returns the number of changed rows as a one-tuple on the
    # UPDATE result. Treat a missing/empty row defensively as zero so a
    # zero-row deactivation still has a sensible return value.
    touched = int(row[0]) if row is not None else 0
    logger.info(
        "annotate.supersession.deactivated",
        table=table,
        new_source_version_id=new_source_version_id,
        rows=touched,
    )
    return touched


__all__ = [
    "deactivate_prior_versions",
]
