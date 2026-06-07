"""``variants_master`` canonical REF/ALT backfill + hom-only recovery.

The second of the post-5.7 backfills (ROADMAP "Post-5.7 backfills"). Phase 2's
alphabetical-ordering normalize (``backend/src/genome/ingest/normalize.py``
``order_alleles``) stores ``variants_master.(ref_allele, alt_allele)`` as
alphabetically-ordered observed bases. Two consequences, both quantified on the
user's real corpus by finding-018:

* **78.3% of rows are hom-only ``ref==alt``** — Phase 2's honest "we don't know
  the reference" encoding for positions where every observation is homozygous.
  These rows match nothing on the 4-tuple coordinate join used by the annotation
  index (and were dropped from imputation per finding-005 #6).
* **~50% of genuine ``ref≠alt`` rows match gnomAD only when ``(ref,alt)`` is
  swapped** — pure alphabetical-order artifact relative to dbSNP's reference
  orientation (finding-018 §2).

This module canonicalizes ``variants_master.(ref_allele, alt_allele)`` against
the **currently-active dbSNP source-version** loaded in sub-phase 5.6: re-orients
the genuine swap victims, recovers hom-only rows by assigning a real ALT from
dbSNP, collapses rows whose new canonical key collides with a sibling at the
same position (re-pointing ``genotype_calls.variant_id`` FKs to the survivor),
and leaves the downstream rebuilds (``genome merge`` →
``align-tier3-consensus`` → ``genome annotate refresh-index``) to re-derive
consensus + index from the canonical state.

Scope **A** (per PR-3 decision): ordering re-orient + hom-only recovery only.
True strand-flipped duplicates (where the two chips observed complementary
allele sets at the same position — the ~106 tier-3 cases in real data) are
intentionally **not** complement-collapsed here; merge tier-3 keeps resolving
them at the genotype level as today, and a small companion command
(``genome annotate align-tier3-consensus``) deletes the now-vestigial
non-canonical-side consensus row post-merge so Phase 6 reads see exactly one
``variant_id`` per real biallelic site. The full strand-flip ``variants_master``
collapse (which would also rewrite ``genotype_calls.allele_1/2`` via
supersession) is deferred to PR 5; finding-005 #1 tracks it explicitly so it
does not become silent drift.

**Why we INSERT new variant_ids for movers instead of UPDATEing ref/alt
in place.** DuckDB's UNIQUE constraint on ``(chrom, pos_grch38, ref_allele,
alt_allele)`` is enforced via the ART index, and an UPDATE that touches an
indexed column is implemented internally as DELETE + INSERT on the index. With
``genotype_calls.variant_id`` declared ``REFERENCES variants_master(variant_id)``
(``ddl/group_1_genotype.sql:117``), even a UPDATE that leaves ``variant_id``
unchanged trips DuckDB's foreign-key check ("key X is still referenced by a
foreign key in a different table"). DuckDB has no ``DISABLE FOREIGN_KEYS``
pragma, no ``ALTER TABLE DROP CONSTRAINT``, and no ``SAVEPOINT``. So the only
mechanic that works is: allocate a fresh ``variant_id`` for each canonical
target key, INSERT the canonical row, re-point ``genotype_calls.variant_id``
to it, then DELETE the old movers (whose FK refs are gone). Unchanged rows
that happen to already sit at a target key are reused as survivors so we don't
introduce avoidable churn.

This means ``variant_id`` is **not preserved** for movers (re-oriented or
recovered rows). The trade-off is acceptable because every consumer of
``variant_id`` is either (a) downstream-regenerated (``consensus_genotypes``,
``discrepancies``, ``variant_annotations_index``), or (b) precondition-empty in
the PR-3 window (the Phase-6/7 derived/insight tables enumerated in
:data:`_PRECONDITION_TABLES`). ``genotype_calls`` is the only consumer that
must be re-pointed, and the FK repoint is the first explicit write step.

**Provenance.** No schema/DDL change (locked). Provenance is captured at the
operation grain by three artifacts: the **pre-mutation file snapshot** of
``genome.duckdb`` taken before the transaction opens (the literal "before"
state, named with the dbSNP version + UTC timestamp), the **structlog
``canonicalize.complete`` event** stamped with the dbSNP ``source_version_id``
(the method version), and the **finding-020** doc that locks the before/after
counts. Row-level "was this canonicalized?" stays derivable by query against
the snapshot + current state. See CLAUDE.md decision #8.

**Supersession.** The mutation is split across **three** transactions, all
forced by the same DuckDB quirk: FK enforcement on a row delete reads the
*pre-transaction* state of the *referencing* table, so an in-transaction DELETE
of those referencing rows is invisible to the check.

* **TX0** ``DELETE FROM discrepancies`` and commits. ``discrepancies`` is the
  only table whose FK points *onto* ``genotype_calls`` (``call_a_id`` /
  ``call_b_id`` -> ``genotype_calls(call_id)``). The TX1 repoint
  ``UPDATE genotype_calls SET variant_id`` is run by DuckDB as delete+reinsert
  of each row, which fires that parent-side check; it must already see
  ``discrepancies`` empty as of a committed transaction, hence TX0.
* **TX1** clears the two ``variants_master``-keyed rollups
  (``consensus_genotypes`` / ``variant_annotations_index``), INSERTs the new
  survivor rows, and re-points ``genotype_calls.variant_id`` to them.
* **TX2** DELETEs the now-orphan old mover rows from ``variants_master`` (the
  same quirk again: the repoint of ``genotype_calls.variant_id`` away from the
  movers must be committed before TX2 so the delete's FK check sees it) and
  recomputes ``has_*_call`` flags.

Crash windows are recoverable within the post-PR-3 runbook: a crash after TX0 /
before TX1 leaves ``discrepancies`` empty with ``variants_master`` unchanged; a
crash after TX1 / before TX2 leaves *harmless* orphan ``variants_master`` rows
(no calls reference them, downstream tables empty). A re-run DELETEs orphans as
a no-survivors-needed pass, and ``merge`` / ``refresh-index`` rebuild the
downstream tables regardless. Note ``_count_fast_path`` detects remaining
``variants_master`` work, not remaining downstream-rebuild work. The
supersession atomicity guarantee (CLAUDE.md decision #7) holds at the
*downstream* boundary — those tables are wholesale-cleared here and re-derived
by ``merge`` / ``refresh-index`` after the canonicalize finishes, so a Phase-6
reader sees either the entire pre- or entire post-canonicalize state across the
operator-driven ``canonicalize -> merge -> refresh-index`` sequence.

It is **not** a registered loader: like :mod:`genome.annotate.index_refresh` and
:mod:`genome.annotate.loaders.variant_aliases`, it is a standalone ``annotate``
subcommand (``canonicalize-variants``), invoked via lazy import from the CLI.
"""

from __future__ import annotations

import contextlib
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog

from genome.annotate.source_versions import get_current_version
from genome.annotate.supersession import commit_and_checkpoint
from genome.config import get_settings
from genome.db.duckdb_conn import _ensure_owner_only, duckdb_connection

if TYPE_CHECKING:
    from pathlib import Path

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)


SOURCE_DB: Final[str] = "dbsnp"
"""Canonical REF/ALT is sourced from the currently-active dbSNP version."""

_BACKUP_SUBDIR: Final[str] = "canonicalize"
"""Subdirectory under settings.archive_path where pre-mutation snapshots land."""

_PRECONDITION_TABLES: Final[tuple[str, ...]] = (
    "vep_consequences",
    "derived_acmg_sf_findings",
    "derived_compound_het",
    "derived_carrier_findings",
    "insight_variants",
)
"""Tables that hold ``variant_id`` and are **not** regenerated by the post-PR-3
``merge`` + ``refresh-index`` runs (Phase 6/7 derivations + insights). The
canonicalize step allocates new ``variant_id``s for movers; if any of these is
non-empty its rows would silently dangle. PR 3 runs in the post-5.7 /
pre-Phase-6 window where all of these are empty by construction; non-zero in
any one is a refuse-and-explain signal.
"""


# ---------------------------------------------------------------------------
# Errors + result.
# ---------------------------------------------------------------------------


class DbsnpNotLoadedError(RuntimeError):
    """Raised when no active dbSNP source-version exists to canonicalize against.

    The dbSNP VCF must be loaded first (``genome annotate refresh --source
    dbsnp``). Without a current pointer there is no canonical REF/ALT source.
    """


class DerivedTablesNotEmptyError(RuntimeError):
    """Raised when a Phase-6/7 table holding ``variant_id`` is non-empty.

    The canonicalize step DELETEs and reassigns ``variant_id``s across
    ``variants_master`` / ``genotype_calls``; the listed tables are not
    regenerated by ``merge`` / ``refresh-index`` and would dangle. The PR-3
    window is post-5.7 / pre-Phase-6; any non-zero count here means the
    operator is running this outside the intended window.
    """


@dataclass(frozen=True, slots=True)
class CanonicalizeResult:
    """Outcome of one :func:`canonicalize_variants` call.

    The new locked drift identifiers for the canonicalize step. Mirrored in
    structlog ``canonicalize.complete`` and the finding-020 lock table.
    """

    dbsnp_source_version_id: int
    already_canonical: bool
    rows_reoriented: int
    rows_recovered_hom_ref: int
    rows_recovered_hom_ref_multialt: int
    rows_recovered_hom_alt: int
    rows_collapsed: int
    calls_repointed: int
    new_variant_ids_allocated: int
    survivors_flag_updated: int
    survivors_enriched: int
    rsid_conflicts: int
    genuine_variants_after: int
    hom_ref_remaining: int
    backup_path: str | None
    wall_clock_seconds: float

    @property
    def rows_changed(self) -> int:
        """Total ``variants_master`` rows whose ``(ref, alt)`` was rewritten."""
        return (
            self.rows_reoriented
            + self.rows_recovered_hom_ref
            + self.rows_recovered_hom_ref_multialt
            + self.rows_recovered_hom_alt
        )


# ---------------------------------------------------------------------------
# Snapshot.
# ---------------------------------------------------------------------------


def _snapshot_filename(dbsnp_version: str | None) -> str:
    """Build a self-identifying snapshot filename: dbSNP version + UTC timestamp."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label = dbsnp_version or "unknown"
    return f"genome.duckdb.pre-canonicalize.dbsnp{label}.{stamp}.bak"


def take_snapshot(
    db_path: Path,
    *,
    archive_root: Path,
    dbsnp_version: str | None,
) -> Path:
    """Snapshot the live DuckDB file to ``archive/canonicalize/<…>.bak``.

    Sequence (per the plan's Q3 refinement #1 — checkpointed consistent state):

    1. Open a read-write connection, issue ``CHECKPOINT`` to fold the WAL into
       the file, close.
    2. ``shutil.copy2`` the file to the timestamped backup path.
    3. ``_ensure_owner_only`` to chmod 0600 (decision #6 — the backup inherits
       the same FDE/perms posture as the live file).

    The destination is under ``archive/canonicalize/`` (gitignored snapshots
    subdir per CLAUDE.md "Common file locations"). Auto-cleanup is manual:
    finding-020 and the runbook state the operator deletes the snapshot once
    the backfill is verified merged. Returns the absolute backup path.
    """
    with duckdb_connection(db_path) as conn:
        conn.execute("CHECKPOINT")

    backup_dir = archive_root / _BACKUP_SUBDIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / _snapshot_filename(dbsnp_version)
    shutil.copy2(db_path, backup_path)
    _ensure_owner_only(backup_path)
    size = backup_path.stat().st_size
    logger.info(
        "canonicalize.backup.created",
        path=str(backup_path),
        size_bytes=size,
        dbsnp_version=dbsnp_version,
    )
    return backup_path


# ---------------------------------------------------------------------------
# SQL constants.
# ---------------------------------------------------------------------------


_DBSNP_ALTS_CTE_BODY: Final[str] = """\
dbsnp_alts AS (
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
)"""
"""Unnested single-base dbSNP alleles under the current dbsnp version pointer.

Inlined verbatim into :data:`_BUILD_CANON_MAP_SQL` and the fast-path
detector (see :func:`_count_fast_path`). The pointer join shape mirrors
:mod:`genome.annotate.index_refresh`. Kept as a module constant rather than
interpolated to satisfy ruff S608 — the two callers each include this body
inside their own static SQL literal.
"""


_BUILD_CANON_MAP_SQL: Final[str] = """
INSERT INTO _canon_map
    (old_variant_id, chrom, pos_grch38, old_ref, old_alt, ref_c, alt_c, mapping_kind)
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
genuine_reorient AS (
    -- ref≠alt, observed allele set == {{dref, some single-base alt_b}}.
    -- Target: (dref, the-other-observed-base). Excludes already-canonical rows.
    SELECT
        vm.variant_id                                            AS old_variant_id,
        vm.chrom,
        vm.pos_grch38,
        vm.ref_allele                                            AS old_ref,
        vm.alt_allele                                            AS old_alt,
        da.dref                                                  AS ref_c,
        CASE WHEN vm.ref_allele = da.dref
             THEN vm.alt_allele
             ELSE vm.ref_allele END                              AS alt_c,
        'genuine_reorient'                                       AS mapping_kind
      FROM variants_master vm
      JOIN dbsnp_alts da
        ON da.chrom = vm.chrom
       AND da.pos_grch38 = vm.pos_grch38
     WHERE vm.variant_type = 'SNV'
       AND vm.ref_allele IN ('A','C','G','T')
       AND vm.alt_allele IN ('A','C','G','T')
       AND vm.ref_allele != vm.alt_allele
       AND da.dref IN (vm.ref_allele, vm.alt_allele)
       AND da.alt_b = CASE WHEN vm.ref_allele = da.dref
                           THEN vm.alt_allele
                           ELSE vm.ref_allele END
),
hom_ref_recover AS (
    -- ref==alt, observed base B == dref. User is hom-ref; pick alphabetically
    -- smallest single-base dbSNP alt (MIN deterministic). Multi-alt yields the
    -- 'hom_ref_recover_multialt' kind for the finding-020 surfacing caveat.
    SELECT
        vm.variant_id                                            AS old_variant_id,
        vm.chrom,
        vm.pos_grch38,
        vm.ref_allele                                            AS old_ref,
        vm.alt_allele                                            AS old_alt,
        vm.ref_allele                                            AS ref_c,
        MIN(da.alt_b)                                            AS alt_c,
        CASE WHEN COUNT(DISTINCT da.alt_b) > 1
             THEN 'hom_ref_recover_multialt'
             ELSE 'hom_ref_recover' END                          AS mapping_kind
      FROM variants_master vm
      JOIN dbsnp_alts da
        ON da.chrom = vm.chrom
       AND da.pos_grch38 = vm.pos_grch38
     WHERE vm.variant_type = 'SNV'
       AND vm.ref_allele IN ('A','C','G','T')
       AND vm.alt_allele IN ('A','C','G','T')
       AND vm.ref_allele = vm.alt_allele
       AND vm.ref_allele = da.dref
     GROUP BY vm.variant_id, vm.chrom, vm.pos_grch38,
              vm.ref_allele, vm.alt_allele
),
hom_alt_recover AS (
    -- ref==alt, observed B != dref, B IN single-base alts. User is hom-alt;
    -- target (dref, B), dosage will resolve to 2 on re-merge.
    SELECT DISTINCT
        vm.variant_id                                            AS old_variant_id,
        vm.chrom,
        vm.pos_grch38,
        vm.ref_allele                                            AS old_ref,
        vm.alt_allele                                            AS old_alt,
        da.dref                                                  AS ref_c,
        vm.ref_allele                                            AS alt_c,
        'hom_alt_recover'                                        AS mapping_kind
      FROM variants_master vm
      JOIN dbsnp_alts da
        ON da.chrom = vm.chrom
       AND da.pos_grch38 = vm.pos_grch38
     WHERE vm.variant_type = 'SNV'
       AND vm.ref_allele IN ('A','C','G','T')
       AND vm.alt_allele IN ('A','C','G','T')
       AND vm.ref_allele = vm.alt_allele
       AND vm.ref_allele != da.dref
       AND vm.ref_allele = da.alt_b
),
all_candidates AS (
    SELECT * FROM genuine_reorient
    UNION ALL SELECT * FROM hom_ref_recover
    UNION ALL SELECT * FROM hom_alt_recover
),
ranked AS (
    -- Deterministic tie-break across kinds for the same old_variant_id (rare:
    -- e.g. a dbSNP record where dref also appears in alt_alleles). The kind
    -- order is the priority: reorient > hom_ref > hom_ref_multi > hom_alt.
    SELECT
        c.*,
        ROW_NUMBER() OVER (
            PARTITION BY old_variant_id
            ORDER BY
                CASE mapping_kind
                    WHEN 'genuine_reorient'         THEN 0
                    WHEN 'hom_ref_recover'          THEN 1
                    WHEN 'hom_ref_recover_multialt' THEN 2
                    WHEN 'hom_alt_recover'          THEN 3
                    ELSE                                 4
                END,
                ref_c, alt_c
        ) AS rn
      FROM all_candidates c
)
SELECT old_variant_id, chrom, pos_grch38, old_ref, old_alt, ref_c, alt_c, mapping_kind
  FROM ranked
 WHERE rn = 1
   AND (ref_c, alt_c) <> (old_ref, old_alt)
"""
"""Single-statement build of the ``_canon_map`` TEMP table.

Per-``old_variant_id`` aggregation with the kind-ordered ``ROW_NUMBER`` keeps
the result deterministic across re-runs (a stable identifier — drift on a
re-run against the same corpus + same dbSNP source-version is a regression
signal).
"""


_BUILD_RESOLVE_SQL: Final[str] = """
INSERT INTO _canon_resolve
    (final_chrom, final_pos, final_ref, final_alt, survivor_id, survivor_is_new,
     representative_old_id)
WITH targets AS (
    SELECT DISTINCT
        chrom AS final_chrom,
        pos_grch38 AS final_pos,
        ref_c AS final_ref,
        alt_c AS final_alt
      FROM _canon_map
),
existing_unchanged AS (
    -- variants_master rows NOT in _canon_map that already sit at a target key.
    SELECT
        t.final_chrom,
        t.final_pos,
        t.final_ref,
        t.final_alt,
        vm.variant_id AS reuse_id
      FROM targets t
      JOIN variants_master vm
        ON vm.chrom        = t.final_chrom
       AND vm.pos_grch38   = t.final_pos
       AND vm.ref_allele   = t.final_ref
       AND vm.alt_allele   = t.final_alt
     WHERE vm.variant_id NOT IN (SELECT old_variant_id FROM _canon_map)
),
representatives AS (
    -- Pick MIN(old_variant_id) per target key as the metadata source for any
    -- newly-INSERTed row. Deterministic and re-runnable.
    SELECT
        chrom AS final_chrom,
        pos_grch38 AS final_pos,
        ref_c AS final_ref,
        alt_c AS final_alt,
        MIN(old_variant_id) AS representative_old_id
      FROM _canon_map
     GROUP BY 1, 2, 3, 4
),
joined AS (
    SELECT
        t.final_chrom,
        t.final_pos,
        t.final_ref,
        t.final_alt,
        eu.reuse_id,
        r.representative_old_id
      FROM targets t
      LEFT JOIN existing_unchanged eu
        ON eu.final_chrom = t.final_chrom
       AND eu.final_pos   = t.final_pos
       AND eu.final_ref   = t.final_ref
       AND eu.final_alt   = t.final_alt
      JOIN representatives r
        ON r.final_chrom = t.final_chrom
       AND r.final_pos   = t.final_pos
       AND r.final_ref   = t.final_ref
       AND r.final_alt   = t.final_alt
),
allocator AS (
    -- Allocate fresh variant_ids only for target keys with no existing reuse.
    -- The base offset is the current MAX(variant_id) on variants_master so the
    -- allocation never collides with existing IDs (works in both production —
    -- where variant_id_seq is in sync — and tests where manual INSERTs leave
    -- the sequence behind). Deterministic order via the key columns.
    SELECT
        j.*,
        (SELECT COALESCE(MAX(variant_id), 0) FROM variants_master)
          + ROW_NUMBER() OVER (
                ORDER BY j.final_chrom, j.final_pos, j.final_ref, j.final_alt
            )                              AS allocated_id
      FROM joined j
     WHERE j.reuse_id IS NULL
)
SELECT
    j.final_chrom,
    j.final_pos,
    j.final_ref,
    j.final_alt,
    COALESCE(j.reuse_id, a.allocated_id) AS survivor_id,
    j.reuse_id IS NULL                   AS survivor_is_new,
    j.representative_old_id              AS representative_old_id
  FROM joined j
  LEFT JOIN allocator a
    ON a.final_chrom = j.final_chrom
   AND a.final_pos   = j.final_pos
   AND a.final_ref   = j.final_ref
   AND a.final_alt   = j.final_alt
"""
"""Per-target-key plan: which ``variant_id`` survives, whether it's new, and
which old row's metadata to copy if new.

Reuse the existing unchanged sibling's id (collapse the canon_map members into
it) when one sits at the target key; otherwise allocate a fresh BIGINT from
``MAX(variant_id) + ROW_NUMBER()`` (the project's idempotent app-allocator
pattern, mirroring ``_next_dbsnp_id`` etc.). The ``MAX``-based base sidesteps
DuckDB sequence-state drift when tests INSERT explicit ids.
"""


_BUILD_REMAP_SQL: Final[str] = """
INSERT INTO _canon_remap (old_variant_id, new_variant_id)
SELECT cm.old_variant_id, r.survivor_id
  FROM _canon_map cm
  JOIN _canon_resolve r
    ON r.final_chrom = cm.chrom
   AND r.final_pos   = cm.pos_grch38
   AND r.final_ref   = cm.ref_c
   AND r.final_alt   = cm.alt_c
"""
"""Per-old-variant-id, the new ``variant_id`` it maps to.

Drives the ``genotype_calls.variant_id`` re-point. The mapping is always
many-to-one (multiple old ids collapse into one survivor in the collision
case; one-to-one for non-collision movers).
"""


_BUILD_CANON_BEST_SQL: Final[str] = """
INSERT INTO _canon_best (survivor_id, best_rsid, distinct_rsids)
SELECT
    rm.new_variant_id                               AS survivor_id,
    arg_min(vm.rsid, vm.variant_id)
        FILTER (WHERE vm.rsid IS NOT NULL)          AS best_rsid,
    COUNT(DISTINCT vm.rsid)                          AS distinct_rsids
  FROM _canon_remap rm
  JOIN variants_master vm ON vm.variant_id = rm.old_variant_id
 GROUP BY rm.new_variant_id
"""
"""Per survivor, the best mover rsid + the distinct-rsid count.

Aggregates over ``_canon_remap`` — the ready-made mover->survivor edge list, one
row per mover — joined to the (still-present) mover rows in ``variants_master``.
Must run in TX1 *before* ``_DELETE_OLD_VARIANTS_SQL`` (TX2) so the movers still
exist, and before ``_INSERT_NEW_SURVIVORS_SQL`` (which LEFT JOINs this table).

``arg_min(rsid, variant_id) FILTER (WHERE rsid IS NOT NULL)`` = the non-NULL rsid
on the lowest old ``variant_id`` (deterministic via the unique PK; NULL when
every mover rsid is NULL) — mirrors the repo idiom in ``index_refresh.py``.
``distinct_rsids`` (``COUNT(DISTINCT)`` ignores NULLs) drives the
``rsid_conflicts`` identifier. Both collapse paths consume ``best_rsid``: the
new-survivor INSERT via ``COALESCE`` and the reuse survivor via the TX2
enrichment UPDATE, closing the finding-020 rsID-loss.
"""


_INSERT_NEW_SURVIVORS_SQL: Final[str] = """
INSERT INTO variants_master
    (variant_id, rsid, chrom, pos_grch38, pos_grch37,
     ref_allele, alt_allele, variant_type,
     has_genotyped_call, has_imputed_call, is_acmg_sf,
     gene_symbols, liftover_chain, liftover_status)
SELECT
    r.survivor_id,
    COALESCE(b.best_rsid, rep.rsid),
    r.final_chrom,
    r.final_pos,
    rep.pos_grch37,
    r.final_ref,
    r.final_alt,
    rep.variant_type,
    FALSE,                  -- has_genotyped_call recomputed below
    FALSE,                  -- has_imputed_call recomputed below
    rep.is_acmg_sf,
    rep.gene_symbols,
    rep.liftover_chain,
    rep.liftover_status
  FROM _canon_resolve r
  JOIN variants_master rep ON rep.variant_id = r.representative_old_id
  LEFT JOIN _canon_best b ON b.survivor_id = r.survivor_id
 WHERE r.survivor_is_new
"""
"""INSERT one new ``variants_master`` row per target key that has no existing
unchanged sibling.

Copies the representative's metadata (pos_grch37, variant_type, etc.) verbatim;
only ``ref_allele`` / ``alt_allele`` are the canonical values from
``_canon_resolve``. The ``rsid`` is the best non-NULL rsid across *all* movers
collapsing into this survivor (``_canon_best.best_rsid``, lowest-``variant_id``
non-NULL wins), falling back to the representative's own rsid when every mover
is NULL — the rep is ``MIN(old_variant_id)`` and rsid-blind, so a higher-id
mover's rsid would otherwise be lost (finding-020 rsID-preservation invariant).
``has_*_call`` flags are recomputed by :func:`_recompute_survivor_flags` once the
FK repoint is done.
"""


_REPOINT_GENOTYPE_CALLS_SQL: Final[str] = """
UPDATE genotype_calls AS gc
   SET variant_id = rm.new_variant_id
  FROM _canon_remap rm
 WHERE gc.variant_id = rm.old_variant_id
   AND gc.variant_id != rm.new_variant_id
"""
"""Re-point every ``genotype_calls.variant_id`` from the old mover id to its
new survivor.

The new survivor row was INSERTed earlier in the same TX1, so the *child-side*
FK (``genotype_calls.variant_id`` -> ``variants_master``) is satisfied (the
existence check reads-its-own-writes). But ``variant_id`` is itself an FK column,
so DuckDB executes this UPDATE as delete+reinsert of each ``genotype_calls`` row,
which fires the *parent-side* FK from ``discrepancies(call_a_id / call_b_id)`` ->
``genotype_calls(call_id)``. DuckDB's FK enforcement reads pre-transaction state,
so ``discrepancies`` must already be empty as of a prior committed transaction —
that is why it is pre-cleared in TX0 (the same snapshot quirk that forces the
TX1/TX2 split). The ``!=`` guard skips no-op self-maps defensively.
"""


_DELETE_OLD_VARIANTS_SQL: Final[str] = """
DELETE FROM variants_master
 WHERE variant_id IN (SELECT old_variant_id FROM _canon_map)
"""
"""Remove the old mover rows.

Safe now: their ``genotype_calls`` have been re-pointed (no inbound FK refs)
and the downstream tables that reference ``variants_master`` were already
DELETEd — ``discrepancies`` in TX0, ``consensus_genotypes`` /
``variant_annotations_index`` in TX1. The Phase-6/7 derived/insight tables are
precondition-empty.
"""


_RECOMPUTE_FLAGS_SQL: Final[str] = """
UPDATE variants_master AS vm
   SET has_genotyped_call = COALESCE(f.has_geno, FALSE),
       has_imputed_call   = COALESCE(f.has_imp,  FALSE)
  FROM (
        SELECT gc.variant_id,
               BOOL_OR(gc.is_active
                       AND gc.source IN ('23andme','ancestry'))
                                                            AS has_geno,
               BOOL_OR(gc.is_active
                       AND gc.source IN ('beagle_imputed',
                                         'topmed_imputed')) AS has_imp
          FROM genotype_calls gc
         WHERE gc.variant_id IN (SELECT survivor_id FROM _canon_resolve)
         GROUP BY gc.variant_id
       ) f
 WHERE vm.variant_id = f.variant_id
"""
"""Recompute ``has_genotyped_call`` / ``has_imputed_call`` on every survivor.

Authoritative recompute from ``genotype_calls`` rather than OR-merge — handles
both flag-up (new calls absorbed) and flag-down (the survivor was new and has
not been seeded with flags yet) cases. The UPDATE touches columns NOT in any
UNIQUE constraint, so it doesn't trip the FK problem.
"""


_ENRICH_REUSE_RSID_SQL: Final[str] = """
UPDATE variants_master AS vm
   SET rsid = COALESCE(vm.rsid, b.best_rsid)
  FROM _canon_resolve r
  JOIN _canon_best b ON b.survivor_id = r.survivor_id
 WHERE vm.variant_id = r.survivor_id
   AND NOT r.survivor_is_new
   AND vm.rsid IS NULL
   AND b.best_rsid IS NOT NULL
"""
"""Fill a NULL rsid on a *reused* survivor from its movers' best rsid.

The new-survivor path inherits ``best_rsid`` at INSERT time; this UPDATE is the
reuse-path counterpart — a chip swap-victim mover carrying a real rsid collapses
into an existing NULL-rsid imputed sibling whose ``variant_id`` is reused as the
survivor, and without this the chip rsid is lost (the dominant ~100K case behind
finding-020's rsID-loss). The survivor's own non-NULL rsid always wins (the
``vm.rsid IS NULL`` guard, belt-and-suspenders with ``COALESCE``); movers only
fill a NULL. A reuse survivor is never itself a mover (``existing_unchanged``
filters ``variant_id NOT IN _canon_map``), so this can never touch a doomed row.

Runs in TX2 next to ``_RECOMPUTE_FLAGS_SQL``, but unlike the flag recompute it is
*not* intrinsically FK-safe: ``rsid`` carries the plain ``idx_vm_rsid`` index, and
DuckDB delete+reinserts a row when an UPDATE touches an indexed column, firing the
parent-side ``genotype_calls.variant_id`` FK check on a survivor that has calls.
The orchestrator therefore drops ``idx_vm_rsid`` (committed) before TX2 and rebuilds
it after — an *in-TX* drop is invisible to DuckDB's pre-transaction FK check, so the
drop must precede the transaction. ``_RECOMPUTE_FLAGS_SQL`` is exempt only because
``has_*_call`` are unindexed.
"""


# ---------------------------------------------------------------------------
# Phase helpers.
# ---------------------------------------------------------------------------


def _check_preconditions(conn: DuckDBPyConnection) -> None:
    """Refuse if any precondition-table holds a row.

    Mirrors the variant_aliases ``DbsnpNotLoadedError`` guard pattern: fail
    fast, before any download/snapshot/mutation.
    """
    offenders: list[tuple[str, int]] = []
    for table in _PRECONDITION_TABLES:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 — module constant
        n = int(row[0]) if row is not None else 0
        if n > 0:
            offenders.append((table, n))
    if not offenders:
        return
    listing = ", ".join(f"{t}={n}" for t, n in offenders)
    msg = (
        f"variants_master canonicalize refuses to run with non-empty Phase-6/7 "
        f"tables that hold variant_id ({listing}). These tables are not "
        f"regenerated by `merge` / `refresh-index`, so reassigning variant_ids "
        f"would silently dangle their rows. PR 3 runs in the post-5.7 / "
        f"pre-Phase-6 window; a non-zero count means you are running this "
        f"outside the intended window."
    )
    raise DerivedTablesNotEmptyError(msg)


def _count_fast_path(conn: DuckDBPyConnection) -> int:
    """Count rows the mutation would actually change (the fast-path detector).

    Runs a stripped-down version of the candidate logic and returns a single
    ``COUNT(*)``. Zero means the table is already canonical and the snapshot +
    mutation can be skipped (intrinsic idempotence).
    """
    sql = """
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
    genuine_reorient AS (
        SELECT vm.variant_id
          FROM variants_master vm
          JOIN dbsnp_alts da
            ON da.chrom = vm.chrom AND da.pos_grch38 = vm.pos_grch38
         WHERE vm.variant_type = 'SNV'
           AND vm.ref_allele IN ('A','C','G','T')
           AND vm.alt_allele IN ('A','C','G','T')
           AND vm.ref_allele != vm.alt_allele
           AND da.dref IN (vm.ref_allele, vm.alt_allele)
           AND da.alt_b = CASE WHEN vm.ref_allele = da.dref
                               THEN vm.alt_allele
                               ELSE vm.ref_allele END
           AND (da.dref,
                CASE WHEN vm.ref_allele = da.dref
                     THEN vm.alt_allele
                     ELSE vm.ref_allele END) <> (vm.ref_allele, vm.alt_allele)
    ),
    hom_recover AS (
        SELECT vm.variant_id
          FROM variants_master vm
          JOIN dbsnp_alts da
            ON da.chrom = vm.chrom AND da.pos_grch38 = vm.pos_grch38
         WHERE vm.variant_type = 'SNV'
           AND vm.ref_allele IN ('A','C','G','T')
           AND vm.alt_allele IN ('A','C','G','T')
           AND vm.ref_allele = vm.alt_allele
           AND (vm.ref_allele = da.dref OR vm.ref_allele = da.alt_b)
    )
    SELECT COUNT(*) FROM (
        SELECT variant_id FROM genuine_reorient
        UNION
        SELECT variant_id FROM hom_recover
    ) c
    """
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row is not None else 0


def _create_temp_tables(conn: DuckDBPyConnection) -> None:
    """``DROP IF EXISTS`` + ``CREATE TEMP TABLE`` for the four staging tables."""
    conn.execute("DROP TABLE IF EXISTS _canon_map")
    conn.execute(
        """
        CREATE TEMP TABLE _canon_map (
            old_variant_id  BIGINT,
            chrom           chromosome_enum,
            pos_grch38      BIGINT,
            old_ref         VARCHAR,
            old_alt         VARCHAR,
            ref_c           VARCHAR,
            alt_c           VARCHAR,
            mapping_kind    VARCHAR
        )
        """,
    )
    conn.execute("DROP TABLE IF EXISTS _canon_resolve")
    conn.execute(
        """
        CREATE TEMP TABLE _canon_resolve (
            final_chrom            chromosome_enum,
            final_pos              BIGINT,
            final_ref              VARCHAR,
            final_alt              VARCHAR,
            survivor_id            BIGINT,
            survivor_is_new        BOOLEAN,
            representative_old_id  BIGINT
        )
        """,
    )
    conn.execute("DROP TABLE IF EXISTS _canon_remap")
    conn.execute(
        """
        CREATE TEMP TABLE _canon_remap (
            old_variant_id  BIGINT,
            new_variant_id  BIGINT
        )
        """,
    )
    conn.execute("DROP TABLE IF EXISTS _canon_best")
    conn.execute(
        """
        CREATE TEMP TABLE _canon_best (
            survivor_id    BIGINT,
            best_rsid      VARCHAR,
            distinct_rsids INTEGER
        )
        """,
    )


def _drop_temp_tables(conn: DuckDBPyConnection) -> None:
    """Mirror ``_create_temp_tables`` — clean up at end of transaction."""
    conn.execute("DROP TABLE IF EXISTS _canon_best")
    conn.execute("DROP TABLE IF EXISTS _canon_remap")
    conn.execute("DROP TABLE IF EXISTS _canon_resolve")
    conn.execute("DROP TABLE IF EXISTS _canon_map")


def _count_map_kinds(conn: DuckDBPyConnection) -> dict[str, int]:
    """Return per-kind counts of ``_canon_map`` rows for the result dataclass."""
    rows = conn.execute(
        "SELECT mapping_kind, COUNT(*) FROM _canon_map GROUP BY mapping_kind",
    ).fetchall()
    return {str(kind): int(n) for kind, n in rows}


def _count_resolve(conn: DuckDBPyConnection) -> tuple[int, int]:
    """Return ``(canon_map_count, new_survivors_count)`` for the rows_collapsed math."""
    map_row = conn.execute("SELECT COUNT(*) FROM _canon_map").fetchone()
    new_row = conn.execute(
        "SELECT COUNT(*) FROM _canon_resolve WHERE survivor_is_new",
    ).fetchone()
    return (
        int(map_row[0]) if map_row is not None else 0,
        int(new_row[0]) if new_row is not None else 0,
    )


def _count_rsid_metadata(conn: DuckDBPyConnection) -> tuple[int, int]:
    """Return ``(survivors_enriched, rsid_conflicts)`` for the rsID-inheritance fix.

    Computed in TX1 while movers + pre-state reuse survivors are intact, because
    DuckDB UPDATE reports rowcount = -1 (see the repoint count above) so the
    reuse-UPDATE's effect must be precounted. ``survivors_enriched`` gates on the
    reuse survivor's *own* ``vm.rsid`` (joined via ``survivor_id``, not the
    representative), exactly mirroring ``_ENRICH_REUSE_RSID_SQL``'s WHERE clause.
    ``rsid_conflicts`` counts survivors where the movers themselves disagree
    (``distinct_rsids > 1``) or a reuse survivor's own non-NULL rsid disagrees
    with the picked best — surfaced, never silently dropped (lowest-id wins).
    """
    enriched_row = conn.execute(
        """
        SELECT COUNT(*)
          FROM _canon_resolve r
          JOIN _canon_best b      ON b.survivor_id = r.survivor_id
          JOIN variants_master vm ON vm.variant_id = r.survivor_id
         WHERE NOT r.survivor_is_new
           AND vm.rsid IS NULL
           AND b.best_rsid IS NOT NULL
        """,
    ).fetchone()
    conflicts_row = conn.execute(
        """
        SELECT COUNT(*)
          FROM _canon_resolve r
          JOIN _canon_best b ON b.survivor_id = r.survivor_id
          LEFT JOIN variants_master vm
            ON vm.variant_id = r.survivor_id AND NOT r.survivor_is_new
         WHERE b.distinct_rsids > 1
            OR (NOT r.survivor_is_new
                AND vm.rsid IS NOT NULL
                AND b.best_rsid IS NOT NULL
                AND vm.rsid <> b.best_rsid)
        """,
    ).fetchone()
    return (
        int(enriched_row[0]) if enriched_row is not None else 0,
        int(conflicts_row[0]) if conflicts_row is not None else 0,
    )


def _count_post(conn: DuckDBPyConnection) -> tuple[int, int]:
    """Return ``(genuine_variants_after, hom_ref_remaining)``."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE ref_allele != alt_allele),
            COUNT(*) FILTER (WHERE ref_allele = alt_allele)
          FROM variants_master
         WHERE variant_type = 'SNV'
        """,
    ).fetchone()
    if row is None:
        return (0, 0)
    return (int(row[0]), int(row[1]))


def _resync_variant_id_sequence(conn: DuckDBPyConnection) -> None:
    """Advance ``variant_id_seq`` so its next value exceeds ``MAX(variant_id)``.

    The allocator assigns survivor ids explicitly as ``MAX + ROW_NUMBER`` and
    never touches the sequence, so the default-``nextval`` ingest path
    (writer.py / imputation.ingest) would otherwise collide. DuckDB has no
    usable reset (``CREATE OR REPLACE SEQUENCE`` trips the column-DEFAULT
    dependency; ``ALTER SEQUENCE … RESTART`` is unimplemented), so we advance
    by draining ``nextval`` to the high-water mark.
    """
    mx_row = conn.execute("SELECT COALESCE(MAX(variant_id), 0) FROM variants_master").fetchone()
    mx = int(mx_row[0]) if mx_row is not None else 0
    seq_row = conn.execute(
        "SELECT last_value, start_value, increment_by "
        "FROM duckdb_sequences() WHERE sequence_name = 'variant_id_seq'",
    ).fetchone()
    if seq_row is None:
        return
    last_value, start_value, increment_by = seq_row
    consumed = int(last_value) if last_value is not None else int(start_value) - int(increment_by)
    delta = mx - consumed
    if delta > 0:
        conn.execute(
            f"SELECT max(s) FROM (SELECT nextval('variant_id_seq') AS s FROM range({delta}))",  # noqa: S608 — integer delta only
        ).fetchone()


# ---------------------------------------------------------------------------
# Top-level entrypoint.
# ---------------------------------------------------------------------------


def _build_already_canonical_result(
    target_svid: int,
    started: float,
    *,
    conn: DuckDBPyConnection,
) -> CanonicalizeResult:
    """Construct a no-op result when the fast-path detector reports zero work."""
    genuine_after, hom_remaining = _count_post(conn)
    wall = time.monotonic() - started
    return CanonicalizeResult(
        dbsnp_source_version_id=target_svid,
        already_canonical=True,
        rows_reoriented=0,
        rows_recovered_hom_ref=0,
        rows_recovered_hom_ref_multialt=0,
        rows_recovered_hom_alt=0,
        rows_collapsed=0,
        calls_repointed=0,
        new_variant_ids_allocated=0,
        survivors_flag_updated=0,
        survivors_enriched=0,
        rsid_conflicts=0,
        genuine_variants_after=genuine_after,
        hom_ref_remaining=hom_remaining,
        backup_path=None,
        wall_clock_seconds=wall,
    )


def canonicalize_variants(  # noqa: PLR0915 — one orchestrator, intentional phase order
    conn: DuckDBPyConnection | None = None,
    *,
    force: bool = False,
    no_backup: bool = False,
) -> CanonicalizeResult:
    """Canonicalize ``variants_master.(ref_allele, alt_allele)`` against dbSNP.

    Pipeline:

    1. Resolve the current dbSNP ``source_version_id`` (fail fast with
       :class:`DbsnpNotLoadedError` if no pointer).
    2. Refuse if any Phase-6/7 variant_id-holding table is non-empty
       (:class:`DerivedTablesNotEmptyError`).
    3. Fast-path detector: if zero rows need work and ``force`` is not set,
       short-circuit and return ``already_canonical=True`` with no snapshot
       and no mutation.
    4. When ``conn is None`` and ``no_backup`` is False: take a pre-mutation
       file snapshot of ``genome.duckdb`` (CHECKPOINT → cp → chmod 0600). A
       borrowed connection (tests) skips the snapshot by construction.
    5. In **three** transactions (the DuckDB FK-on-DELETE enforcement forces the
       split — see the module docstring). TX0: ``DELETE FROM discrepancies`` and
       commit (it FK-references ``genotype_calls``, which the TX1 repoint
       delete+reinserts). TX1: stage ``_canon_map`` / ``_canon_resolve`` /
       ``_canon_remap``, ``DELETE`` the two ``variants_master``-keyed rollups
       that will be regenerated, INSERT new ``variants_master`` rows for target
       keys with no existing unchanged sibling, re-point
       ``genotype_calls.variant_id`` to the survivors, ``commit``. TX2: ``DELETE``
       the now-orphan old mover rows (``_DELETE_OLD_VARIANTS_SQL``, keyed off the
       still-live ``_canon_map`` TEMP), recompute survivor flags, then re-sync
       ``variant_id_seq`` past the explicitly-allocated survivor ids so the
       default-``nextval`` ingest path can't collide, ``commit_and_checkpoint``.
       On any exception in any transaction ``conn.rollback()`` and re-raise.
    6. Return the locked drift identifiers.
    """
    started = time.monotonic()
    settings = get_settings()

    # ---- Phase 1+2: preflight (borrowed or owned read-write connection) ----
    ctx: contextlib.AbstractContextManager[DuckDBPyConnection] = (
        duckdb_connection() if conn is None else contextlib.nullcontext(conn)
    )
    with ctx as preflight_conn:
        current = get_current_version(preflight_conn, SOURCE_DB)
        if current is None:
            msg = (
                "no active dbSNP source-version; load the dbSNP VCF first via "
                "`genome annotate refresh --source dbsnp` before canonicalizing "
                "variants_master."
            )
            raise DbsnpNotLoadedError(msg)
        target_svid = current.source_version_id
        log = logger.bind(source_version_id=target_svid, force=force)
        log.info("canonicalize.start", dbsnp_version=current.version)

        _check_preconditions(preflight_conn)

        work_remaining = _count_fast_path(preflight_conn)
        log.info("canonicalize.fast_path", work_remaining=work_remaining)
        if work_remaining == 0 and not force:
            return _build_already_canonical_result(
                target_svid,
                started,
                conn=preflight_conn,
            )

    # ---- Phase 3: snapshot (only when we own the connection) ----
    backup_path: Path | None = None
    if conn is None and not no_backup:
        backup_path = take_snapshot(
            settings.genome_duckdb_path,
            archive_root=settings.archive_path,
            dbsnp_version=current.version,
        )

    # ---- Phase 4: mutation. TWO transactions; see module docstring on the
    # DuckDB FK enforcement that forces the split. Both run inside the same
    # owned-or-borrowed connection so a test that wants to inspect intermediate
    # state can pass conn=... and reuse it.
    mutation_ctx: contextlib.AbstractContextManager[DuckDBPyConnection] = (
        duckdb_connection() if conn is None else contextlib.nullcontext(conn)
    )
    with mutation_ctx as active_conn:
        # ---- TX0: pre-clear ``discrepancies`` in its OWN committed transaction.
        # ``discrepancies`` is the only table with an FK *onto* ``genotype_calls``
        # (``call_a_id`` / ``call_b_id`` -> ``genotype_calls(call_id)``). The TX1
        # repoint ``UPDATE genotype_calls SET variant_id`` is executed by DuckDB
        # as delete+reinsert of each row (``variant_id`` carries the ART index of
        # its own FK), which fires that parent-side check. DuckDB's FK enforcement
        # reads *pre-transaction* state, so an in-TX1 ``DELETE FROM discrepancies``
        # is invisible to the check and the repoint trips it — the same quirk that
        # forces the TX1/TX2 split for ``DELETE FROM variants_master``. So this
        # delete must be committed before TX1 opens. The two ``variants_master``-
        # keyed rollups (``consensus_genotypes`` / ``variant_annotations_index``)
        # do NOT need early commit and stay in TX1 to keep their clear atomic with
        # the insert+repoint (decision #7).
        active_conn.begin()
        try:
            active_conn.execute("DELETE FROM discrepancies")
            active_conn.commit()
            log.info("canonicalize.discrepancies_cleared")
        except Exception:
            active_conn.rollback()
            log.exception("canonicalize.tx0_failed")
            raise

        # ---- TX1: stage + clear + INSERT new survivors + re-point FKs ----
        active_conn.begin()
        try:
            _create_temp_tables(active_conn)
            active_conn.execute(_BUILD_CANON_MAP_SQL)
            active_conn.execute(_BUILD_RESOLVE_SQL)
            active_conn.execute(_BUILD_REMAP_SQL)

            kind_counts = _count_map_kinds(active_conn)
            canon_map_count, new_survivors = _count_resolve(active_conn)
            rows_collapsed = canon_map_count - new_survivors
            log.info(
                "canonicalize.mapping_built",
                kinds=kind_counts,
                canon_map=canon_map_count,
                new_survivors=new_survivors,
                rows_collapsed=rows_collapsed,
            )

            # Stage the per-survivor best mover rsid + precount the rsID-
            # inheritance deltas. Must run here: after the mover->survivor edge
            # list (``_canon_remap``) exists, while the movers + pre-state reuse
            # survivors are still present (the DELETE is in TX2), and before
            # ``_INSERT_NEW_SURVIVORS_SQL`` LEFT JOINs ``_canon_best`` below.
            active_conn.execute(_BUILD_CANON_BEST_SQL)
            survivors_enriched, rsid_conflicts = _count_rsid_metadata(active_conn)
            log.info(
                "canonicalize.rsid_inheritance_staged",
                survivors_enriched=survivors_enriched,
                rsid_conflicts=rsid_conflicts,
            )

            # Clear the variants_master-keyed rollups that will be regenerated by
            # the post-PR-3 commands (merge + align + refresh-index).
            # ``discrepancies`` was already cleared in TX0 (it FK-references
            # genotype_calls, not just variants_master — see the TX0 note).
            active_conn.execute("DELETE FROM variant_annotations_index")
            active_conn.execute("DELETE FROM consensus_genotypes")
            log.info("canonicalize.downstream_cleared")

            # INSERT new variants_master rows for target keys with no existing
            # unchanged sibling. Must precede the FK repoint so the new
            # variant_id is a valid FK target.
            active_conn.execute(_INSERT_NEW_SURVIVORS_SQL)
            log.info("canonicalize.new_rows_inserted", count=new_survivors)

            # Count calls about to be re-pointed (DuckDB's UPDATE returns
            # rowcount=-1, so we precompute the count ourselves).
            calls_row = active_conn.execute(
                """
                SELECT COUNT(*)
                  FROM genotype_calls gc
                  JOIN _canon_remap rm
                    ON gc.variant_id = rm.old_variant_id
                 WHERE gc.variant_id != rm.new_variant_id
                """,
            ).fetchone()
            calls_repointed = int(calls_row[0]) if calls_row is not None else 0

            # FK repoint of genotype_calls.variant_id.
            active_conn.execute(_REPOINT_GENOTYPE_CALLS_SQL)
            log.info("canonicalize.fk_repointed", calls_repointed=calls_repointed)

            active_conn.commit()
            log.info("canonicalize.tx1_committed")
        except Exception:
            active_conn.rollback()
            log.exception("canonicalize.tx1_failed")
            raise

        # Drop ``idx_vm_rsid`` (committed, before TX2 opens) so the reuse-path
        # rsid enrichment in TX2 doesn't trip DuckDB's parent-side FK check.
        # Updating an *indexed* column on an FK-referenced row delete+reinserts
        # the row, which fires the ``genotype_calls.variant_id`` parent check;
        # ``has_*_call`` (``_RECOMPUTE_FLAGS_SQL``) is exempt only because those
        # columns are unindexed. DuckDB's FK enforcement reads pre-transaction
        # state, so an *in-TX2* DROP is invisible to the check — the drop must be
        # committed first (same quirk that forces the TX split). Recreated in the
        # ``finally`` so a TX2 failure can't strand the DB without the index.
        active_conn.execute("DROP INDEX IF EXISTS idx_vm_rsid")
        try:
            # ---- TX2: DELETE now-orphan old rows + recompute flags ----
            active_conn.begin()
            try:
                # The old mover rows are now orphan (their genotype_calls were
                # re-pointed in TX1, downstream tables cleared). DELETE them keyed
                # off the still-live connection-scoped ``_canon_map`` TEMP — it
                # survives the TX1 commit and is only torn down by
                # ``_drop_temp_tables`` below.
                active_conn.execute(_DELETE_OLD_VARIANTS_SQL)
                log.info("canonicalize.old_rows_deleted", count=canon_map_count)

                # Count survivors whose flags will be recomputed (rowcount unreliable).
                survivors_row = active_conn.execute(
                    """
                    SELECT COUNT(DISTINCT gc.variant_id)
                      FROM genotype_calls gc
                      JOIN _canon_resolve r ON r.survivor_id = gc.variant_id
                    """,
                ).fetchone()
                survivors_updated = int(survivors_row[0]) if survivors_row is not None else 0
                active_conn.execute(_RECOMPUTE_FLAGS_SQL)
                log.info("canonicalize.flags_recomputed", survivors=survivors_updated)

                # Enrich reused survivors' NULL rsid from their movers' best rsid
                # — the reuse-path counterpart to the new-survivor COALESCE. FK-
                # safe only because ``idx_vm_rsid`` was dropped above (see the
                # note); the precount ``survivors_enriched`` mirrors this UPDATE's
                # WHERE clause exactly.
                active_conn.execute(_ENRICH_REUSE_RSID_SQL)
                log.info("canonicalize.rsid_enriched", survivors_enriched=survivors_enriched)

                # The allocator assigned survivor ids explicitly as MAX + ROW_NUMBER
                # and never advanced variant_id_seq; re-sync it so the default-
                # nextval ingest path (writer.py / imputation.ingest) can't collide.
                _resync_variant_id_sequence(active_conn)

                _drop_temp_tables(active_conn)

                commit_and_checkpoint(
                    active_conn,
                    source_name="variants_master_canonicalize",
                )
            except Exception:
                active_conn.rollback()
                log.exception("canonicalize.tx2_failed")
                raise
        finally:
            # Rebuild the rsid index regardless of TX2 outcome (autocommit; the
            # connection is back in autocommit after commit or rollback).
            active_conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_rsid ON variants_master(rsid)")

        genuine_after, hom_remaining = _count_post(active_conn)

    wall = time.monotonic() - started
    result = CanonicalizeResult(
        dbsnp_source_version_id=target_svid,
        already_canonical=False,
        rows_reoriented=kind_counts.get("genuine_reorient", 0),
        rows_recovered_hom_ref=kind_counts.get("hom_ref_recover", 0),
        rows_recovered_hom_ref_multialt=kind_counts.get("hom_ref_recover_multialt", 0),
        rows_recovered_hom_alt=kind_counts.get("hom_alt_recover", 0),
        rows_collapsed=rows_collapsed,
        calls_repointed=calls_repointed,
        new_variant_ids_allocated=new_survivors,
        survivors_flag_updated=survivors_updated,
        survivors_enriched=survivors_enriched,
        rsid_conflicts=rsid_conflicts,
        genuine_variants_after=genuine_after,
        hom_ref_remaining=hom_remaining,
        backup_path=str(backup_path) if backup_path is not None else None,
        wall_clock_seconds=wall,
    )
    log.info(
        "canonicalize.complete",
        dbsnp_source_version_id=target_svid,
        rows_reoriented=result.rows_reoriented,
        rows_recovered_hom_ref=result.rows_recovered_hom_ref,
        rows_recovered_hom_ref_multialt=result.rows_recovered_hom_ref_multialt,
        rows_recovered_hom_alt=result.rows_recovered_hom_alt,
        rows_collapsed=result.rows_collapsed,
        calls_repointed=result.calls_repointed,
        new_variant_ids_allocated=result.new_variant_ids_allocated,
        survivors_flag_updated=result.survivors_flag_updated,
        survivors_enriched=result.survivors_enriched,
        rsid_conflicts=result.rsid_conflicts,
        genuine_variants_after=result.genuine_variants_after,
        hom_ref_remaining=result.hom_ref_remaining,
        backup_path=result.backup_path,
        wall_clock_seconds=round(result.wall_clock_seconds, 2),
    )
    if result.rsid_conflicts > 0:
        log.warning(
            "canonicalize.rsid_conflicts",
            rsid_conflicts=result.rsid_conflicts,
            detail=(
                "distinct non-NULL rsIDs collided on one canonical key; "
                "lowest-variant_id rsid was kept, the other(s) dropped"
            ),
        )
    return result


__all__ = [
    "SOURCE_DB",
    "CanonicalizeResult",
    "DbsnpNotLoadedError",
    "DerivedTablesNotEmptyError",
    "canonicalize_variants",
    "take_snapshot",
]
