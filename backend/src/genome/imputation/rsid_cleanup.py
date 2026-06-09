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
* **Leftover logged, not fatal.** The non-``rs`` / non-``i`` / non-``.`` / non-NULL
  complement can exceed the coordinate-regex matches by a handful of rows —
  legitimate chip-probe IDs (Illumina ``kgp…`` 1000G probe names, vendor ``VGXS…``,
  Ancestry ``acom_…``) that ingest carries in and the regex correctly excludes. The
  pre-flight logs ``matched``, ``complement``, the ``leftover`` count, and a bounded
  distinct sample of those probe IDs for visibility; it does not abort or widen the
  pattern (probe-ID recovery is deferred to ``variant_aliases``).
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

# Bounded, stable, deduplicated peek at the complement rows the coordinate regex
# does NOT match — legitimate chip-probe IDs (``kgp…`` / ``VGXS…`` / ``acom_…``).
# DISTINCT + ORDER BY makes the sample deterministic; LIMIT caps the log line (the
# real leftover is a handful of rows). The regex is the only bind parameter.
_LEFTOVER_SAMPLE_SQL: Final[str] = (
    "SELECT DISTINCT rsid FROM variants_master "
    "WHERE rsid IS NOT NULL AND rsid NOT LIKE 'rs%' "
    "AND rsid NOT LIKE 'i%' AND rsid <> '.' "
    "AND NOT regexp_full_match(rsid, ?) "
    "ORDER BY rsid LIMIT 20"
)


def _scalar(conn: DuckDBPyConnection, sql: str, params: list[str] | None = None) -> int:
    row = conn.execute(sql, params if params is not None else []).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def _sample(conn: DuckDBPyConnection, sql: str, params: list[str]) -> list[str]:
    rows = conn.execute(sql, params).fetchall()
    return [str(row[0]) for row in rows]


def normalize_imputed_rsids(conn: DuckDBPyConnection) -> int:
    """NULL every synthetic ``chrom:pos:ref:alt`` rsid in ``variants_master``.

    Idempotent. Returns the number of rows cleaned. The sweep is positively scoped
    to the coordinate shape, so it never touches real ``rs#``, chip-internal
    ``i####``, or chip-probe IDs (``kgp…`` / ``VGXS…`` / ``acom_…``) that
    Ancestry/23andMe ingest carries in. Those probe IDs sit in the non-``rs`` /
    non-``i`` / non-``.`` / non-NULL complement but are *not* synthetic; the count
    and a bounded sample of that leftover are logged for visibility (their recovery
    is deferred to ``variant_aliases``), not treated as an error.
    """
    matched = _scalar(conn, _COUNT_SYNTHETIC_SQL, [_SYNTHETIC_RSID_REGEX])
    complement = _scalar(conn, _COUNT_COMPLEMENT_SQL)
    # A coordinate string never starts with 'rs' or 'i' and is never '.', so the
    # regex-matched set is a strict subset of the complement: leftover >= 0 without
    # a third scan. The leftover is legitimate chip-probe IDs the regex excludes.
    leftover = complement - matched
    leftover_sample = _sample(conn, _LEFTOVER_SAMPLE_SQL, [_SYNTHETIC_RSID_REGEX])
    # Logged before the matched == 0 early return so the chip-probe residue stays
    # visible in steady state (every clean re-run has matched == 0).
    logger.info(
        "imputation.normalize_rsids.preflight",
        matched=matched,
        complement=complement,
        leftover=leftover,
        leftover_sample=leftover_sample,
    )
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
