"""Post-merge tier-3 consensus alignment companion to ``canonicalize-variants``.

PR-3 Scope-A leaves the strand-flipped ``variants_master`` duplicates (the
~106 tier-3 cases in real data) un-collapsed: the side whose allele set matches
dbSNP gets canonicalized; the complement-only sibling stays as-is and matches
nothing on the index. ``merge._apply_strand_flip`` then writes
``consensus_genotypes`` for **both** ``variant_id``s in the pair (each pair-
rewrite increments the count twice, so ``strand_flip_resolutions=106`` = 53
pairs x 2 row-rewrites). The result is a split: consensus lives on both
variant_ids, but annotations only on the canonical one — Phase 6 reads would
see 106 variant_ids with ``consensus_genotypes`` and no
``variant_annotations_index`` row.

This module is the minimal alignment per the PR-3 Q1 answer: it identifies
pairs of ``variants_master`` rows at the same ``(chrom, pos_grch38)`` where
both ``consensus_genotypes`` rows have ``consensus_method='disagreement_resolved'``,
determines which side matches a dbSNP ``(chrom, pos, ref, alt)`` 4-tuple (the
canonical side), and ``DELETE``s the ``consensus_genotypes`` row on the
non-canonical side. The non-canonical ``variants_master`` row becomes a
vestigial row with ``genotype_calls`` but no ``consensus_genotypes`` — Phase 6
reads from consensus and won't see it. The surviving canonical consensus's
``contributing_calls`` already references both call_ids, so no information is
lost.

It is **not** a registered loader; it's a standalone ``annotate`` subcommand
(``align-tier3-consensus``), invoked via lazy import from the CLI. The full
``variants_master``-level strand-flip collapse (which would also rewrite
``genotype_calls.allele_1/2`` via supersession to keep dosage consistent) is
deferred to PR 5; finding-005 #1 tracks it.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from genome.annotate.source_versions import get_current_version
from genome.annotate.supersession import commit_and_checkpoint
from genome.db.duckdb_conn import duckdb_connection

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)


SOURCE_DB: Final[str] = "dbsnp"
"""Canonical-side classification is read through the dbSNP pointer."""


# ---------------------------------------------------------------------------
# Errors + result.
# ---------------------------------------------------------------------------


class DbsnpNotLoadedError(RuntimeError):
    """Raised when no active dbSNP source-version exists to classify against."""


@dataclass(frozen=True, slots=True)
class AlignResult:
    """Outcome of one :func:`align_tier3_consensus` call.

    ``rows_deleted`` is the number of ``consensus_genotypes`` rows removed
    (the non-canonical sides of tier-3 strand-flip pairs). One row is removed
    per pair (the canonical side keeps its consensus). On a re-run against
    already-aligned data this is zero (intrinsic idempotence).
    """

    dbsnp_source_version_id: int
    pairs_examined: int
    rows_deleted: int
    wall_clock_seconds: float


# ---------------------------------------------------------------------------
# SQL.
# ---------------------------------------------------------------------------


_FIND_NON_CANONICAL_SQL: Final[str] = """
WITH dbsnp_alts AS (
    SELECT
        d.chrom        AS chrom,
        d.pos_grch38   AS pos_grch38,
        d.ref_allele   AS dref,
        u.alt_b        AS alt_b
      FROM dbsnp_annotations d
      JOIN annotation_sources s
        ON s.source_db = 'dbsnp'
       AND s.current_source_version_id = d.source_version_id
      CROSS JOIN UNNEST(d.alt_alleles) AS u(alt_b)
     WHERE lower(d.variant_class) = 'snv'
       AND d.ref_allele IN ('A','C','G','T')
       AND u.alt_b      IN ('A','C','G','T')
       AND d.pos_grch38 IS NOT NULL
),
disagreement_pairs AS (
    -- Two distinct variants_master rows at the same (chrom, pos), each
    -- carrying a consensus_method='disagreement_resolved' row. This is the
    -- post-merge shape of a tier-3 strand-flip pair under Scope A: merge
    -- writes consensus on both, the canonical side matches dbSNP, the
    -- complement-only side does not.
    SELECT
        vm.variant_id,
        vm.chrom,
        vm.pos_grch38,
        vm.ref_allele,
        vm.alt_allele
      FROM variants_master vm
      JOIN consensus_genotypes cg
        ON cg.variant_id = vm.variant_id
       AND cg.consensus_method = 'disagreement_resolved'
),
pair_sized AS (
    -- Restrict to (chrom, pos) buckets that hold at least two such rows.
    SELECT
        dp.variant_id,
        dp.chrom,
        dp.pos_grch38,
        dp.ref_allele,
        dp.alt_allele,
        COUNT(*) OVER (PARTITION BY dp.chrom, dp.pos_grch38) AS bucket_size
      FROM disagreement_pairs dp
),
classified AS (
    -- A row is "canonical" iff it matches dbSNP on the 4-tuple (some
    -- single-base alt of dbSNP equals the row's alt_allele AND dbSNP's ref
    -- equals the row's ref_allele). Use LEFT JOIN + IS NOT NULL so rows
    -- with no dbSNP match register as non-canonical.
    SELECT
        ps.variant_id,
        ps.chrom,
        ps.pos_grch38,
        ps.ref_allele,
        ps.alt_allele,
        ps.bucket_size,
        BOOL_OR(da.alt_b IS NOT NULL) AS is_canonical
      FROM pair_sized ps
      LEFT JOIN dbsnp_alts da
        ON da.chrom = ps.chrom
       AND da.pos_grch38 = ps.pos_grch38
       AND da.dref = ps.ref_allele
       AND da.alt_b = ps.alt_allele
     WHERE ps.bucket_size >= 2
     GROUP BY ps.variant_id, ps.chrom, ps.pos_grch38, ps.ref_allele,
              ps.alt_allele, ps.bucket_size
)
SELECT variant_id, chrom, pos_grch38, ref_allele, alt_allele, is_canonical
  FROM classified
"""
"""Identify the variant_ids on each side of a post-merge tier-3 strand-flip pair.

Returns one row per variant_id at a same-position bucket with
``consensus_method='disagreement_resolved'``, flagged ``is_canonical`` when its
(chrom, pos, ref, alt) matches the current dbSNP record. The caller DELETEs
``consensus_genotypes`` for every ``is_canonical=FALSE`` variant_id in a bucket
that also contains an ``is_canonical=TRUE`` variant_id — i.e. only when a
clean canonical/non-canonical split exists. Same-bucket pairs that are both
non-canonical (no dbSNP match either way) are left alone; that's not a
strand-flip we can resolve here.
"""


# ---------------------------------------------------------------------------
# Top-level entrypoint.
# ---------------------------------------------------------------------------


def _resolve_dead_ids(conn: DuckDBPyConnection) -> tuple[int, list[int]]:
    """Return ``(pairs_examined, non_canonical_variant_ids_to_delete)``.

    Reads the classifier query and groups by ``(chrom, pos_grch38)``: when a
    bucket contains both a canonical (matches dbSNP) and a non-canonical side,
    the non-canonical variant_id is targeted for deletion. ``pairs_examined``
    counts the buckets that had ≥1 canonical AND ≥1 non-canonical sides — the
    actionable pairs. Buckets that are all-canonical or all-non-canonical are
    skipped.
    """
    rows = conn.execute(_FIND_NON_CANONICAL_SQL).fetchall()
    by_pos: dict[tuple[str, int], list[tuple[int, bool]]] = {}
    for variant_id, chrom, pos_grch38, _ref, _alt, is_canonical in rows:
        by_pos.setdefault(
            (str(chrom), int(pos_grch38)),
            [],
        ).append((int(variant_id), bool(is_canonical)))

    dead_ids: list[int] = []
    pairs_examined = 0
    for members in by_pos.values():
        if len(members) < 2:  # noqa: PLR2004 — pairs need ≥2 sides
            continue
        canonical = [vid for vid, c in members if c]
        non_canonical = [vid for vid, c in members if not c]
        if canonical and non_canonical:
            pairs_examined += 1
            dead_ids.extend(non_canonical)
    return pairs_examined, dead_ids


def align_tier3_consensus(
    conn: DuckDBPyConnection | None = None,
) -> AlignResult:
    """Delete non-canonical-side ``consensus_genotypes`` rows for tier-3 pairs.

    Idempotent: a second run against already-aligned data finds no actionable
    pairs and reports ``rows_deleted=0``. Fails fast with
    :class:`DbsnpNotLoadedError` if no dbSNP pointer (canonical-side
    classification has nothing to lean on).
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
                "`genome annotate refresh --source dbsnp` before aligning "
                "tier-3 consensus."
            )
            raise DbsnpNotLoadedError(msg)
        target_svid = current.source_version_id
        log = logger.bind(source_version_id=target_svid)
        log.info("align_tier3.start", dbsnp_version=current.version)

        pairs_examined, dead_ids = _resolve_dead_ids(active_conn)
        log.info(
            "align_tier3.classified",
            pairs_examined=pairs_examined,
            non_canonical_count=len(dead_ids),
        )

        if not dead_ids:
            wall = time.monotonic() - started
            log.info(
                "align_tier3.complete",
                pairs_examined=pairs_examined,
                rows_deleted=0,
                wall_clock_seconds=round(wall, 2),
            )
            return AlignResult(
                dbsnp_source_version_id=target_svid,
                pairs_examined=pairs_examined,
                rows_deleted=0,
                wall_clock_seconds=wall,
            )

        active_conn.begin()
        try:
            active_conn.execute("DROP TABLE IF EXISTS _align_dead_ids")
            active_conn.execute(
                "CREATE TEMP TABLE _align_dead_ids (variant_id BIGINT)",
            )
            values_sql = ", ".join(f"({vid})" for vid in dead_ids)
            active_conn.execute(
                f"INSERT INTO _align_dead_ids (variant_id) VALUES {values_sql}",  # noqa: S608 — integers only
            )
            active_conn.execute(
                """
                DELETE FROM consensus_genotypes
                 WHERE variant_id IN (SELECT variant_id FROM _align_dead_ids)
                """,
            )
            active_conn.execute("DROP TABLE IF EXISTS _align_dead_ids")
            commit_and_checkpoint(
                active_conn,
                source_name="align_tier3_consensus",
            )
        except Exception:
            active_conn.rollback()
            log.exception("align_tier3.failed")
            raise

    wall = time.monotonic() - started
    log.info(
        "align_tier3.complete",
        pairs_examined=pairs_examined,
        rows_deleted=len(dead_ids),
        wall_clock_seconds=round(wall, 2),
    )
    return AlignResult(
        dbsnp_source_version_id=target_svid,
        pairs_examined=pairs_examined,
        rows_deleted=len(dead_ids),
        wall_clock_seconds=wall,
    )


__all__ = [
    "SOURCE_DB",
    "AlignResult",
    "DbsnpNotLoadedError",
    "align_tier3_consensus",
]
