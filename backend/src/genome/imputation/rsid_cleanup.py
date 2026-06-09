"""One-time remediation: NULL synthetic ``chrom:pos:ref:alt`` rsids (finding-021).

Phase-4 imputation ingest copied Beagle's synthetic VCF ``ID`` — a
``chrom:pos:ref:alt`` coordinate string emitted for panel variants with no dbSNP
rsID (e.g. ``14:29619977:C:T``) — verbatim into ``variants_master.rsid``. This
sweep NULLs those already-persisted strings. It is the existing-data counterpart
to the strict ``^rs[0-9]+$`` predicate now applied at the ingest assignment site
(:func:`genome.imputation.ingest._dbsnp_rsid_or_none`), which keeps future
imports clean. ``--force-reimport`` is *not* the cleaning mechanism: the import
upsert inserts only variants not already present and never rewrites an existing
row's rsid, so a re-import of the same corpus leaves the persisted strings in
place.

Safety:

* **Positively scoped.** The sweep matches the synthetic ``chrom:pos:ref:alt``
  shape directly, never the negation of ``rs%``. Real ``rs<n>`` and chip-internal
  ``i####`` IDs carry no colon and can never match.
* **Pre-flight equality check.** Before mutating, the count of regex-matched rows
  must equal the non-``rs`` / non-``i`` / non-``.`` / non-NULL population; a
  mismatch aborts (the regex must match every synthetic ID and nothing else).
* **Index drop/rebuild.** The bulk UPDATE touches the indexed (``idx_vm_rsid``),
  FK-referenced ``rsid`` column. DuckDB delete+reinserts a row when an indexed
  column changes, which trips the ``genotype_calls.variant_id`` parent FK check
  against pre-transaction state. The index is dropped (committed) before the
  UPDATE and rebuilt in a ``finally`` — the same quirk the canonicalize backfill
  handles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)

# Beagle's synthetic IDs look like ``14:29619977:C:T``. The colon-delimited
# four-field structure is what real ``rs<n>`` / chip ``i####`` IDs never carry,
# so even a permissive allele class cannot over-match them. ``regexp_full_match``
# anchors the whole string. Passed as a bind parameter (no SQL interpolation).
_SYNTHETIC_RSID_REGEX: Final[str] = r"(chr)?([0-9]{1,2}|X|Y|MT):[0-9]+:[ACGTN]+:[ACGTN]+"

_COUNT_SYNTHETIC_SQL: Final[str] = (
    "SELECT COUNT(*) FROM variants_master WHERE rsid IS NOT NULL AND regexp_full_match(rsid, ?)"
)

# The population that is not a real rs#, not a chip i####, not the '.' sentinel,
# and not NULL — exactly the set the synthetic regex must match, no more, no less.
_COUNT_COMPLEMENT_SQL: Final[str] = (
    "SELECT COUNT(*) FROM variants_master "
    "WHERE rsid IS NOT NULL AND rsid NOT LIKE 'rs%' "
    "AND rsid NOT LIKE 'i%' AND rsid <> '.'"
)

_NULL_SYNTHETIC_SQL: Final[str] = (
    "UPDATE variants_master SET rsid = NULL WHERE rsid IS NOT NULL AND regexp_full_match(rsid, ?)"
)


def _scalar(conn: DuckDBPyConnection, sql: str, params: list[str] | None = None) -> int:
    row = conn.execute(sql, params if params is not None else []).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def normalize_imputed_rsids(conn: DuckDBPyConnection) -> int:
    """NULL every synthetic ``chrom:pos:ref:alt`` rsid in ``variants_master``.

    Idempotent. Returns the number of rows cleaned. Raises :class:`RuntimeError`
    if the pre-flight count of regex-matched rows disagrees with the
    non-``rs`` / non-``i`` / non-``.`` / non-NULL population — the regex must
    match every synthetic ID and nothing else, so a mismatch is a stop sign, not
    a cue to widen or narrow the pattern blindly.
    """
    matched = _scalar(conn, _COUNT_SYNTHETIC_SQL, [_SYNTHETIC_RSID_REGEX])
    complement = _scalar(conn, _COUNT_COMPLEMENT_SQL)
    if matched != complement:
        msg = (
            f"synthetic-rsid regex matched {matched} rows but the "
            f"non-rs / non-i / non-'.' population is {complement}; aborting "
            f"before mutating — the regex must match every synthetic ID and "
            f"nothing else."
        )
        raise RuntimeError(msg)
    logger.info("imputation.normalize_rsids.preflight", matched=matched)
    if matched == 0:
        return 0

    # idx_vm_rsid must be dropped — and the drop committed — before the UPDATE.
    # Changing an indexed column on an FK-referenced row makes DuckDB
    # delete+reinsert it, firing the genotype_calls.variant_id parent check
    # against pre-transaction state (an in-transaction DROP is invisible to it).
    # Rebuilt in the finally so a failure can't strand the table un-indexed.
    conn.execute("DROP INDEX IF EXISTS idx_vm_rsid")
    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(_NULL_SYNTHETIC_SQL, [_SYNTHETIC_RSID_REGEX])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_rsid ON variants_master(rsid)")

    logger.info("imputation.normalize_rsids.complete", rows_cleaned=matched)
    return matched
