"""Same-SNP duplicate ``variants_master`` collapse (PR 5b — closes finding-005 #1).

PR-3's Scope-A canonicalize (:mod:`genome.annotate.canonicalize`) deliberately
left same-SNP duplicates un-collapsed: one physical biallelic SNP stored as ≥2
``variants_master`` rows at the same ``(chrom, pos)``. The interim
``align-tier3-consensus`` patch only deleted the non-canonical side's
``consensus_genotypes`` row; the duplicate row — with its own ``genotype_calls``
and annotation joins — survived and would duplicate Phase-6 reads.

Read-only real-data measurement (finding-026/027) showed the residual is **not**
the assumed clean biallelic strand-flips (only 5 exist), but ≈684 duplicates
across five mechanisms:

* **no-call** — a no-call ``(N,N)`` placeholder row beside a real biallelic
  sibling (the dominant population; hidden from the prior code by an
  ``ACGT``-before-``COUNT`` filter — root cause RC2);
* **swap** — same allele set, REF/ALT order reversed (both non-canonical);
* **strandflip** — reverse-complement biallelic pair (one dbSNP-canonical);
* **hom-opposite-strand** — a real-hom row on the opposite strand;
* **hom-same-strand** — a real-hom row on the same strand.

The prior code matched only the strandflip pattern (``complement_pair``-equal with
exactly one canonical — root cause RC1). This module generalizes to all five.

**Identification is per-EDGE, not per-bucket.** At each ``(chrom, pos)`` bucket of
≥2 SNV rows, the dbSNP-canonical biallelic rows with *different* allele sets are
the legit multi-allelic alts — they are **protected** (never collapsed onto each
other). A single survivor is picked for the remaining (duplicate) rows, and each
duplicate is reconciled against it:

* **repoint** (no-call / swap / hom-same): the call's observed alleles already lie
  on the survivor's strand, so the call rides ``_REPOINT_ALL_CALLS_SQL`` verbatim —
  no new call, no supersession.
* **complement** (strandflip / hom-opposite): the call is on the opposite strand,
  so a new active call with ``complement_pair`` alleles is INSERTed on the survivor
  (``strand_status='flipped_to_match'``) and the old call is deactivated +
  superseded (decision #7 — never UPDATE an active call's alleles).
* **DROP** (a no-call at a multi-allelic position with **no single survivor**): the
  ``(N,N)`` no-call has nowhere correct to repoint, so its call + row are deleted
  (no repoint onto an arbitrary alt); its locus rsID is coalesced onto a canonical
  sibling whose ``rsid`` is NULL.

The routing is confirmed against **call content**: the structural mechanism gives an
expected strand (same for swap/hom-same, opposite for strandflip/hom-opposite) and
each call's observed alleles must agree; a call that resolves under neither strand
(an internally inconsistent row) **skips the edge** and is counted
``genotype_mismatch_skipped`` rather than guessed.

**Guards.** A reconciliation that would give the survivor two *active* calls of the
same ``source`` (the one-active-per-``(variant, source)`` invariant) skips the edge
and is counted ``source_collision_skipped``. Palindromic survivors (A/T, C/G) are
skipped (swap vs strand-flip is undecidable from genotype). There is **no**
no-imputed-call guard (the prior code's): imputed calls relocate to the survivor
exactly like chip calls.

**Dependency.** The no-call repoints re-merge to ``imputed_only`` (genotype
preserved) only because the ``consensus_v1`` chip-no-call fix (finding-028, PR
5b-pre) landed first; without it, ``merge`` would clobber ≈523 imputed genotypes.

**Transaction scaffold = canonicalize's TX0/TX1/TX2 + the ``idx_vm_rsid`` drop
dance.** TX0 clears ``discrepancies`` (FK onto ``genotype_calls.call_id``). TX1
clears the two ``variants_master``-keyed rollups, INSERTs complemented calls,
re-points every call on each reconciled dead, deactivates + supersedes the old
chip calls, and DELETEs the dropped ``(N,N)`` rows' calls. ``idx_vm_rsid`` is
dropped (committed) before TX2 so the rsID coalesce on an FK-referenced survivor
doesn't trip the parent-side check, and rebuilt in a ``finally``. TX2 coalesces
survivor rsIDs, deletes the orphan reconciled + dropped rows, and recomputes
``has_*_call``. No ``variant_id_seq`` resync — this allocates no new ``variant_id``s.

It is **not** a registered loader: like :mod:`genome.annotate.canonicalize` and
:mod:`genome.annotate.align_tier3` it is a standalone ``annotate`` subcommand
(``collapse-duplicate-variants``), invoked via lazy import from the CLI.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import structlog

from genome.annotate.canonicalize import take_snapshot
from genome.annotate.source_versions import get_current_version
from genome.annotate.supersession import commit_and_checkpoint
from genome.config import get_settings
from genome.db.duckdb_conn import duckdb_connection
from genome.merge.strand import (
    complement,
    complement_pair,
    is_palindromic_site,
)

if TYPE_CHECKING:
    from pathlib import Path

    from duckdb import DuckDBPyConnection

logger = structlog.get_logger(__name__)


SOURCE_DB: Final[str] = "dbsnp"
"""Canonical-side classification is read through the dbSNP pointer."""

_SNAPSHOT_SUBDIR: Final[str] = "strand-collapse"
_SNAPSHOT_LABEL: Final[str] = "strand-collapse"
_SUPERSEDED_REASON: Final[str] = "strand_flip_collapse_pr5"

_BASES: Final[frozenset[str]] = frozenset("ACGT")
_CHIP_SOURCES: Final[frozenset[str]] = frozenset({"23andme", "ancestry"})

_MIN_BUCKET: Final[int] = 2
_MULTIALLELIC_ALT_SETS: Final[int] = 2
"""≥ this many distinct canonical allele sets at a position ⇒ legit multi-allelic."""

# (new_call_id, old_call_id, survivor_id, source, source_chip_version,
#  ingestion_run_id, genotype_raw, allele_1, allele_2, is_imputed,
#  imputation_r2, imputation_panel) — one staging row for ``_sc_new_calls``.
_NewCallRow = tuple[
    int, int, int, str, str | None, int, str, str, str, bool, float | None, str | None
]


# ---------------------------------------------------------------------------
# Errors + result.
# ---------------------------------------------------------------------------


class DbsnpNotLoadedError(RuntimeError):
    """Raised when no active dbSNP source-version exists to classify against."""


@dataclass(frozen=True, slots=True)
class CollapseEdge:
    """One actionable collapse onto a survivor (or a DROP), for the dry-run printout."""

    survivor_id: int
    survivor_rsid: str | None
    survivor_ref: str
    survivor_alt: str
    mechanism: str
    dead_variant_ids: tuple[int, ...]
    dead_rsids: tuple[str | None, ...]
    calls_complemented: int


@dataclass(frozen=True, slots=True)
class StrandCollapseResult:
    """Outcome of one :func:`collapse_duplicate_variants` call (the locked drift identifiers).

    On a ``--dry-run`` every mutation counter is zero, ``backup_path`` is None, and
    ``edges`` carries the actionable set the real run *would* collapse.
    """

    dbsnp_source_version_id: int
    dry_run: bool
    actionable_edges: int
    calls_complemented: int
    calls_repointed: int
    variants_master_deleted: int
    rsid_coalesced: int
    rsid_conflicts: int
    # Per-mechanism breakdown.
    no_call_repointed: int
    no_call_dropped: int
    swaps_collapsed: int
    strandflips_collapsed: int
    hom_opp_collapsed: int
    hom_same_collapsed: int
    legit_multiallelic_skipped: int
    genotype_mismatch_skipped: int
    source_collision_skipped: int
    degenerate_skipped: int
    backup_path: str | None
    wall_clock_seconds: float
    edges: tuple[CollapseEdge, ...]


# ---------------------------------------------------------------------------
# Internal identification structures.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Row:
    """One classified ``variants_master`` row in a same-position bucket."""

    variant_id: int
    ref: str
    alt: str
    rsid: str | None
    is_canonical: bool

    @property
    def is_nocall(self) -> bool:
        return self.ref == "N" and self.alt == "N"

    @property
    def is_biallelic(self) -> bool:
        return self.ref != self.alt and self.ref in _BASES and self.alt in _BASES

    @property
    def is_hom(self) -> bool:
        return self.ref == self.alt and self.ref in _BASES


@dataclass(frozen=True, slots=True)
class _Call:
    """One active ``genotype_calls`` row on a bucket row."""

    call_id: int
    source: str
    source_chip_version: str | None
    ingestion_run_id: int
    allele_1: str | None
    allele_2: str | None
    is_no_call: bool
    is_imputed: bool
    imputation_r2: float | None
    imputation_panel: str | None


@dataclass(frozen=True, slots=True)
class _DeadEdge:
    """A reconciled duplicate ``N`` and the complement-INSERTs it contributes."""

    variant_id: int
    rsid: str | None
    mechanism: str
    new_calls: tuple[_NewCallRow, ...]


@dataclass(frozen=True, slots=True)
class _Actionable:
    """A vetted single-survivor collapse: survivor ``C`` + ≥1 reconciled dead ``N``."""

    survivor_id: int
    survivor_ref: str
    survivor_alt: str
    survivor_rsid: str | None
    deads: tuple[_DeadEdge, ...]


@dataclass(frozen=True, slots=True)
class _Drop:
    """A no-call ``(N,N)`` dropped at a multi-allelic position (no single survivor)."""

    drop_variant_id: int
    drop_rsid: str | None
    coalesce_target_id: int


@dataclass(slots=True)
class _Counters:
    """Per-mechanism + skip tallies accumulated during identification."""

    no_call_repointed: int = 0
    no_call_dropped: int = 0
    swaps_collapsed: int = 0
    strandflips_collapsed: int = 0
    hom_opp_collapsed: int = 0
    hom_same_collapsed: int = 0
    legit_multiallelic_skipped: int = 0
    genotype_mismatch_skipped: int = 0
    source_collision_skipped: int = 0
    degenerate_skipped: int = 0

    def bump(self, mechanism: str) -> None:
        if mechanism == "no_call":
            self.no_call_repointed += 1
        elif mechanism == "swap":
            self.swaps_collapsed += 1
        elif mechanism == "strandflip":
            self.strandflips_collapsed += 1
        elif mechanism == "hom_opp":
            self.hom_opp_collapsed += 1
        elif mechanism == "hom_same":
            self.hom_same_collapsed += 1


@dataclass(slots=True)
class _Identification:
    """The full read-only identification result feeding the plan."""

    actionables: list[_Actionable] = field(default_factory=list)
    drops: list[_Drop] = field(default_factory=list)
    counters: _Counters = field(default_factory=_Counters)


# ---------------------------------------------------------------------------
# SQL.
# ---------------------------------------------------------------------------


_CLASSIFY_SQL: Final[str] = """
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
bucket AS (
    SELECT
        vm.variant_id,
        vm.chrom,
        vm.pos_grch38,
        vm.ref_allele,
        vm.alt_allele,
        vm.rsid,
        COUNT(*) OVER (PARTITION BY vm.chrom, vm.pos_grch38) AS bucket_size
      FROM variants_master vm
     WHERE vm.variant_type = 'SNV'
)
SELECT
    b.variant_id,
    CAST(b.chrom AS VARCHAR)          AS chrom,
    b.pos_grch38,
    b.ref_allele,
    b.alt_allele,
    b.rsid,
    BOOL_OR(da.alt_b IS NOT NULL)     AS is_canonical
  FROM bucket b
  LEFT JOIN dbsnp_alts da
    ON da.chrom      = b.chrom
   AND da.pos_grch38 = b.pos_grch38
   AND da.dref       = b.ref_allele
   AND da.alt_b      = b.alt_allele
 WHERE b.bucket_size >= 2
 GROUP BY b.variant_id, b.chrom, b.pos_grch38, b.ref_allele, b.alt_allele, b.rsid
"""
"""Classify every ``variants_master`` SNV row in a same-position bucket of ≥2 rows.

Unlike the prior version, the ``bucket`` CTE does **not** filter on ACGT *before*
the partitioned ``COUNT`` — so no-call ``(N,N)`` rows count toward ``bucket_size``
and their real biallelic sibling is no longer dropped to ``bucket_size = 1`` (root
cause RC2). ``is_canonical`` is TRUE only when ``(ref, alt)`` matches a current
dbSNP 4-tuple, which by construction only ACGT-distinct rows can do.
"""


_REPOINT_ALL_CALLS_SQL: Final[str] = """
UPDATE genotype_calls AS gc
   SET variant_id = rm.survivor_id
  FROM _sc_remap rm
 WHERE gc.variant_id = rm.dead_variant_id
"""
"""Re-point **every** ``genotype_calls`` row on each reconciled dead ``N`` to ``C``.

Not just the complemented chip calls: inactive prior-supersession calls, repoint-
as-is (same-strand) calls, and strand-invariant no-calls all move too, or TX2's
``DELETE`` of ``N`` fails the FK. ``variant_id`` is itself an FK column, so DuckDB
runs this as delete+reinsert of each row, firing the parent-side ``discrepancies``
FK — which is why ``discrepancies`` is pre-cleared in TX0.
"""


_INSERT_NEW_CALLS_SQL: Final[str] = """
INSERT INTO genotype_calls (
    call_id, variant_id, source, source_chip_version, ingestion_run_id,
    genotype_raw, allele_1, allele_2, is_no_call,
    is_imputed, imputation_r2, imputation_panel,
    raw_strand, strand_status, is_active
)
SELECT
    nc.new_call_id,
    nc.survivor_id,
    nc.source::source_enum,
    nc.source_chip_version,
    nc.ingestion_run_id,
    nc.genotype_raw,
    nc.allele_1,
    nc.allele_2,
    FALSE,
    nc.is_imputed,
    nc.imputation_r2,
    nc.imputation_panel,
    '+',
    'flipped_to_match'::strand_status_enum,
    TRUE
  FROM _sc_new_calls nc
"""
"""INSERT the complemented active calls on the survivors (strandflip / hom-opposite).

Alleles are ``complement_pair(old.allele_1, old.allele_2)`` (re-sorted onto the
canonical plus strand), ``strand_status='flipped_to_match'``. Provenance (``source``
/ ``source_chip_version`` / ``ingestion_run_id`` / imputation fields) is preserved
from the superseded call. Only non-no-call calls that needed complementing are
staged here, so ``is_no_call`` is always FALSE; no-calls and same-strand calls ride
:data:`_REPOINT_ALL_CALLS_SQL` verbatim.
"""


_DEACTIVATE_OLD_CALLS_SQL: Final[str] = """
UPDATE genotype_calls AS gc
   SET is_active = FALSE,
       superseded_by = nc.new_call_id,
       superseded_reason = ?
  FROM _sc_new_calls nc
 WHERE gc.call_id = nc.old_call_id
"""
"""Deactivate + supersede the old complemented calls (the row-grain supersession record).

``variant_id`` was already moved to the survivor by :data:`_REPOINT_ALL_CALLS_SQL`;
this only flips ``is_active`` and backfills the audit fields. The inactive row keeps
its original opposite-strand alleles as the audit record (decision #7 — never UPDATE
an active call's allele content).
"""


_DELETE_DROP_CALLS_SQL: Final[str] = """
DELETE FROM genotype_calls
 WHERE variant_id IN (SELECT drop_variant_id FROM _sc_drop)
"""
"""Delete the no-call ``genotype_calls`` of the DROPped ``(N,N)`` rows.

DROP rows are **not** in ``_sc_remap`` (no repoint onto an arbitrary alt at a
multi-allelic position), so their no-call calls are removed outright. Safe: a
no-call placeholder carries no genotype/insight; ``discrepancies`` is cleared in
TX0 and ``consensus_genotypes`` in TX1, so nothing references these call_ids.
"""


_COALESCE_RSID_SQL: Final[str] = """
UPDATE variants_master AS vm
   SET rsid = s.coalesced_rsid
  FROM _sc_survivors s
 WHERE vm.variant_id = s.survivor_id
   AND vm.rsid IS NULL
   AND s.coalesced_rsid IS NOT NULL
"""
"""Fill a NULL survivor (or DROP coalesce-target) rsID from the best dead rsID.

Runs in TX2 after ``idx_vm_rsid`` is dropped: ``rsid`` would otherwise be an indexed
column whose UPDATE delete+reinserts the (FK-referenced) survivor row. The
``vm.rsid IS NULL`` guard means a survivor's own non-NULL rsID always wins.
"""


_RECOMPUTE_FLAGS_SQL: Final[str] = """
UPDATE variants_master AS vm
   SET has_genotyped_call = COALESCE(f.has_geno, FALSE),
       has_imputed_call   = COALESCE(f.has_imp,  FALSE)
  FROM (
        SELECT gc.variant_id,
               BOOL_OR(gc.is_active
                       AND gc.source IN ('23andme','ancestry'))      AS has_geno,
               BOOL_OR(gc.is_active
                       AND gc.source IN ('beagle_imputed',
                                         'topmed_imputed'))          AS has_imp
          FROM genotype_calls gc
         WHERE gc.variant_id IN (SELECT survivor_id FROM _sc_survivors)
         GROUP BY gc.variant_id
       ) f
 WHERE vm.variant_id = f.variant_id
"""
"""Authoritative recompute of ``has_*_call`` on every survivor from ``genotype_calls``
(absorbs the complemented / repointed calls). ``has_*_call`` are unindexed, so this
UPDATE is FK-safe without the index drop.
"""


_DELETE_DEAD_VARIANTS_SQL: Final[str] = """
DELETE FROM variants_master
 WHERE variant_id IN (SELECT dead_variant_id FROM _sc_remap)
    OR variant_id IN (SELECT drop_variant_id FROM _sc_drop)
"""
"""Remove the orphan reconciled (``_sc_remap``) + dropped (``_sc_drop``) rows. Safe:
reconciled deads' calls were re-pointed in TX1; dropped deads' calls were deleted in
TX1; ``discrepancies`` cleared in TX0; the two rollups cleared in TX1.
"""


# ---------------------------------------------------------------------------
# Identification.
# ---------------------------------------------------------------------------


def _fetch_active_calls(conn: DuckDBPyConnection, variant_ids: list[int]) -> dict[int, list[_Call]]:
    """Fetch every **active** ``genotype_calls`` row on the bucket rows."""
    if not variant_ids:
        return {}
    placeholders = ",".join("?" for _ in variant_ids)
    rows = conn.execute(
        f"""
        SELECT call_id, variant_id, CAST(source AS VARCHAR),
               source_chip_version, ingestion_run_id,
               allele_1, allele_2, is_no_call, is_imputed,
               imputation_r2, imputation_panel
          FROM genotype_calls
         WHERE is_active AND variant_id IN ({placeholders})
         ORDER BY call_id
        """,  # noqa: S608 — ``placeholders`` is only '?' tokens; ids bound as params
        variant_ids,
    ).fetchall()
    out: dict[int, list[_Call]] = {}
    for r in rows:
        out.setdefault(int(r[1]), []).append(
            _Call(
                call_id=int(r[0]),
                source=str(r[2]),
                source_chip_version=None if r[3] is None else str(r[3]),
                ingestion_run_id=int(r[4]),
                allele_1=None if r[5] is None else str(r[5]),
                allele_2=None if r[6] is None else str(r[6]),
                is_no_call=bool(r[7]),
                is_imputed=bool(r[8]),
                imputation_r2=None if r[9] is None else float(r[9]),
                imputation_panel=None if r[10] is None else str(r[10]),
            ),
        )
    return out


def _edge_mechanism(survivor: _Row, n: _Row) -> str | None:  # noqa: PLR0911 — decision tree
    """Structural mechanism of duplicate ``n`` vs survivor ``C`` (None ⇒ not a duplicate).

    None means a legit multi-allelic / unrelated sibling that must be left untouched.
    """
    c_set = frozenset({survivor.ref, survivor.alt})
    if n.is_nocall:
        return "no_call"
    if n.is_hom:
        base = n.ref
        if base in c_set:
            return "hom_same"
        if complement(base) in c_set:
            return "hom_opp"
        return None
    if n.is_biallelic:
        n_set = frozenset({n.ref, n.alt})
        if n_set == c_set:
            return "swap"
        if frozenset(complement(x) for x in n_set) == c_set:
            return "strandflip"
        return None
    return None


def _route_dead(
    survivor: _Row,
    mechanism: str,
    calls: list[_Call],
    base_call_id: int,
) -> tuple[tuple[_NewCallRow, ...], int] | None:
    """Route each active call on ``n`` onto ``C``; return (complement-INSERTs, next id).

    Returns None when the edge must be skipped (a call resolves under neither strand —
    an internally inconsistent row; ``genotype_mismatch``). Repoint-as-is calls
    produce no INSERT (they ride :data:`_REPOINT_ALL_CALLS_SQL`); only complement
    calls are returned.
    """
    c_set = {survivor.ref, survivor.alt}
    expect_opposite = mechanism in {"strandflip", "hom_opp"}
    new_calls: list[_NewCallRow] = []
    next_id = base_call_id
    for call in calls:
        if call.is_no_call:
            continue  # strand-invariant; rides _REPOINT_ALL_CALLS_SQL
        a1, a2 = call.allele_1 or "", call.allele_2 or ""
        obs = {a1, a2}
        same_strand = obs <= c_set
        opp_strand = {complement(x) for x in obs} <= c_set
        if expect_opposite:
            if not opp_strand:
                return None  # edge-label says flip, call content disagrees
            c1, c2 = complement_pair(a1, a2)
            new_calls.append(
                (
                    next_id,
                    call.call_id,
                    survivor.variant_id,
                    call.source,
                    call.source_chip_version,
                    call.ingestion_run_id,
                    f"{c1}{c2}",
                    c1,
                    c2,
                    call.is_imputed,
                    call.imputation_r2,
                    call.imputation_panel,
                ),
            )
            next_id += 1
        elif not same_strand:
            return None  # edge-label says same strand, call content disagrees
        # same-strand real call: repoint as-is (no INSERT)
    return (tuple(new_calls), next_id)


def _classify_bucket(  # noqa: C901, PLR0912 — one branch per survivor/skip case
    members: list[_Row],
    calls_by_vid: dict[int, list[_Call]],
    next_call_id: int,
) -> tuple[_Actionable | None, list[_Drop], int, _Counters]:
    """Classify one same-position bucket into an actionable collapse / drops / skips.

    Returns ``(actionable_or_None, drops, next_call_id, local_counters)``. Edge-level:
    the legit multi-allelic alts are protected, the duplicates reconciled onto a
    single survivor (or DROPped when no single survivor exists).
    """
    counters = _Counters()
    canon = [m for m in members if m.is_canonical]
    canon_sets = {frozenset({m.ref, m.alt}) for m in canon}

    # Legit multi-allelic position: ≥2 canonical alts with different allele sets.
    if len(canon_sets) >= _MULTIALLELIC_ALT_SETS:
        counters.legit_multiallelic_skipped += 1
        drops: list[_Drop] = []
        target = min(canon, key=lambda m: m.variant_id)
        for m in members:
            if m.is_nocall:
                drops.append(
                    _Drop(
                        drop_variant_id=m.variant_id,
                        drop_rsid=m.rsid,
                        coalesce_target_id=target.variant_id,
                    ),
                )
                counters.no_call_dropped += 1
            elif not m.is_canonical:
                # A real-genotype duplicate at a multi-allelic position: ambiguous,
                # surface rather than guess.
                counters.genotype_mismatch_skipped += 1
        return (None, drops, next_call_id, counters)

    # Pick a single survivor for the duplicates.
    if len(canon) == 1:
        survivor = canon[0]
    else:
        biallelic = [m for m in members if m.is_biallelic]
        if not biallelic:
            counters.degenerate_skipped += 1
            return (None, [], next_call_id, counters)
        survivor = min(
            biallelic,
            key=lambda m: (not _has_active_chip(calls_by_vid.get(m.variant_id, [])), m.variant_id),
        )

    if is_palindromic_site(survivor.ref, survivor.alt):
        return (None, [], next_call_id, counters)

    survivor_sources = {c.source for c in calls_by_vid.get(survivor.variant_id, [])}
    deads: list[_DeadEdge] = []
    nid = next_call_id
    for n in members:
        if n.variant_id == survivor.variant_id:
            continue
        mechanism = _edge_mechanism(survivor, n)
        if mechanism is None:
            continue  # legit multi-allelic / unrelated sibling — leave untouched
        n_calls = calls_by_vid.get(n.variant_id, [])
        if survivor_sources & {c.source for c in n_calls}:
            counters.source_collision_skipped += 1
            continue
        routed = _route_dead(survivor, mechanism, n_calls, nid)
        if routed is None:
            counters.genotype_mismatch_skipped += 1
            continue
        new_calls, nid = routed
        deads.append(
            _DeadEdge(
                variant_id=n.variant_id,
                rsid=n.rsid,
                mechanism=mechanism,
                new_calls=new_calls,
            ),
        )
        survivor_sources |= {c.source for c in n_calls}
        counters.bump(mechanism)

    if not deads:
        return (None, [], nid, counters)
    actionable = _Actionable(
        survivor_id=survivor.variant_id,
        survivor_ref=survivor.ref,
        survivor_alt=survivor.alt,
        survivor_rsid=survivor.rsid,
        deads=tuple(deads),
    )
    return (actionable, [], nid, counters)


def _has_active_chip(calls: list[_Call]) -> bool:
    return any(c.source in _CHIP_SOURCES for c in calls)


def _accumulate(total: _Counters, local: _Counters) -> None:
    total.no_call_repointed += local.no_call_repointed
    total.no_call_dropped += local.no_call_dropped
    total.swaps_collapsed += local.swaps_collapsed
    total.strandflips_collapsed += local.strandflips_collapsed
    total.hom_opp_collapsed += local.hom_opp_collapsed
    total.hom_same_collapsed += local.hom_same_collapsed
    total.legit_multiallelic_skipped += local.legit_multiallelic_skipped
    total.genotype_mismatch_skipped += local.genotype_mismatch_skipped
    total.source_collision_skipped += local.source_collision_skipped
    total.degenerate_skipped += local.degenerate_skipped


def _identify(conn: DuckDBPyConnection, base_call_id: int) -> _Identification:
    """Return the per-edge collapse plan (read-only; the ``--dry-run`` core)."""
    rows = conn.execute(_CLASSIFY_SQL).fetchall()
    buckets: dict[tuple[str, int], list[_Row]] = {}
    all_ids: list[int] = []
    for variant_id, chrom, pos, ref, alt, rsid, is_canonical in rows:
        vid = int(variant_id)
        all_ids.append(vid)
        buckets.setdefault((str(chrom), int(pos)), []).append(
            _Row(
                variant_id=vid,
                ref=str(ref),
                alt=str(alt),
                rsid=None if rsid is None else str(rsid),
                is_canonical=bool(is_canonical),
            ),
        )

    calls_by_vid = _fetch_active_calls(conn, all_ids)
    result = _Identification()
    nid = base_call_id
    for members in buckets.values():
        if len(members) < _MIN_BUCKET:
            continue
        actionable, drops, nid, local = _classify_bucket(members, calls_by_vid, nid)
        if actionable is not None:
            result.actionables.append(actionable)
        result.drops.extend(drops)
        _accumulate(result.counters, local)
    return result


# ---------------------------------------------------------------------------
# Plan (Python-side) → staged temp tables.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Plan:
    """Flat, staging-ready view of the collapse derived from the identification."""

    new_calls: list[_NewCallRow]
    remap: list[tuple[int, int]]
    survivors: list[tuple[int, str | None]]
    drop_ids: list[int]
    counters: _Counters
    rsid_coalesced: int
    rsid_conflicts: int
    edges: list[CollapseEdge]

    @property
    def calls_complemented(self) -> int:
        return len(self.new_calls)


def _build_plan(ident: _Identification) -> _Plan:
    """Translate the identification into staging rows + rsID-coalesce decisions."""
    new_calls: list[_NewCallRow] = []
    remap: list[tuple[int, int]] = []
    survivors: list[tuple[int, str | None]] = []
    drop_ids: list[int] = []
    edges: list[CollapseEdge] = []
    rsid_coalesced = 0
    rsid_conflicts = 0

    for bucket in ident.actionables:
        dead_rsids: list[str] = []
        complemented = 0
        for dead in bucket.deads:
            remap.append((dead.variant_id, bucket.survivor_id))
            new_calls.extend(dead.new_calls)
            complemented += len(dead.new_calls)
            if dead.rsid is not None:
                dead_rsids.append(dead.rsid)
        best = dead_rsids[0] if dead_rsids else None
        survivors.append((bucket.survivor_id, best))
        if bucket.survivor_rsid is None and best is not None:
            rsid_coalesced += 1
        if len(set(dead_rsids)) > 1 or (
            bucket.survivor_rsid is not None and best is not None and bucket.survivor_rsid != best
        ):
            rsid_conflicts += 1
        edges.append(
            CollapseEdge(
                survivor_id=bucket.survivor_id,
                survivor_rsid=bucket.survivor_rsid,
                survivor_ref=bucket.survivor_ref,
                survivor_alt=bucket.survivor_alt,
                mechanism="+".join(sorted({d.mechanism for d in bucket.deads})),
                dead_variant_ids=tuple(d.variant_id for d in bucket.deads),
                dead_rsids=tuple(d.rsid for d in bucket.deads),
                calls_complemented=complemented,
            ),
        )

    for drop in ident.drops:
        drop_ids.append(drop.drop_variant_id)
        if drop.drop_rsid is not None:
            # Locus rsID preservation: _COALESCE_RSID_SQL fills the canonical sibling
            # only when its rsid is NULL. Not counted in rsid_coalesced (best-effort,
            # not a collapse-survivor coalesce).
            survivors.append((drop.coalesce_target_id, drop.drop_rsid))
        edges.append(
            CollapseEdge(
                survivor_id=drop.coalesce_target_id,
                survivor_rsid=None,
                survivor_ref="",
                survivor_alt="",
                mechanism="drop",
                dead_variant_ids=(drop.drop_variant_id,),
                dead_rsids=(drop.drop_rsid,),
                calls_complemented=0,
            ),
        )

    return _Plan(
        new_calls=new_calls,
        remap=remap,
        survivors=survivors,
        drop_ids=drop_ids,
        counters=ident.counters,
        rsid_coalesced=rsid_coalesced,
        rsid_conflicts=rsid_conflicts,
        edges=edges,
    )


def _create_temp_tables(conn: DuckDBPyConnection) -> None:
    conn.execute("DROP TABLE IF EXISTS _sc_new_calls")
    conn.execute(
        """
        CREATE TEMP TABLE _sc_new_calls (
            new_call_id          BIGINT,
            old_call_id          BIGINT,
            survivor_id          BIGINT,
            source               VARCHAR,
            source_chip_version  VARCHAR,
            ingestion_run_id     BIGINT,
            genotype_raw         VARCHAR,
            allele_1             VARCHAR,
            allele_2             VARCHAR,
            is_imputed           BOOLEAN,
            imputation_r2        DOUBLE,
            imputation_panel     VARCHAR
        )
        """,
    )
    conn.execute("DROP TABLE IF EXISTS _sc_remap")
    conn.execute(
        "CREATE TEMP TABLE _sc_remap (dead_variant_id BIGINT, survivor_id BIGINT)",
    )
    conn.execute("DROP TABLE IF EXISTS _sc_drop")
    conn.execute("CREATE TEMP TABLE _sc_drop (drop_variant_id BIGINT)")
    conn.execute("DROP TABLE IF EXISTS _sc_survivors")
    conn.execute(
        "CREATE TEMP TABLE _sc_survivors (survivor_id BIGINT, coalesced_rsid VARCHAR)",
    )


def _drop_temp_tables(conn: DuckDBPyConnection) -> None:
    conn.execute("DROP TABLE IF EXISTS _sc_survivors")
    conn.execute("DROP TABLE IF EXISTS _sc_drop")
    conn.execute("DROP TABLE IF EXISTS _sc_remap")
    conn.execute("DROP TABLE IF EXISTS _sc_new_calls")


def _stage_plan(conn: DuckDBPyConnection, plan: _Plan) -> None:
    """Insert the Python-built plan rows into the staging temp tables."""
    for row in plan.new_calls:
        conn.execute(
            """
            INSERT INTO _sc_new_calls
                (new_call_id, old_call_id, survivor_id, source, source_chip_version,
                 ingestion_run_id, genotype_raw, allele_1, allele_2,
                 is_imputed, imputation_r2, imputation_panel)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            list(row),
        )
    for dead_variant_id, survivor_id in plan.remap:
        conn.execute(
            "INSERT INTO _sc_remap (dead_variant_id, survivor_id) VALUES (?, ?)",
            [dead_variant_id, survivor_id],
        )
    for drop_variant_id in plan.drop_ids:
        conn.execute(
            "INSERT INTO _sc_drop (drop_variant_id) VALUES (?)",
            [drop_variant_id],
        )
    for survivor_id, coalesced_rsid in plan.survivors:
        conn.execute(
            "INSERT INTO _sc_survivors (survivor_id, coalesced_rsid) VALUES (?, ?)",
            [survivor_id, coalesced_rsid],
        )


def _next_call_id_base(conn: DuckDBPyConnection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(call_id), 0) FROM genotype_calls").fetchone()
    return (int(row[0]) if row is not None else 0) + 1


# ---------------------------------------------------------------------------
# Top-level entrypoint.
# ---------------------------------------------------------------------------


def collapse_duplicate_variants(
    conn: DuckDBPyConnection | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    no_backup: bool = False,
) -> StrandCollapseResult:
    """Collapse same-SNP duplicate ``variants_master`` rows (closes finding-005 #1).

    Pipeline:

    1. Resolve the current dbSNP ``source_version_id`` (fail fast with
       :class:`DbsnpNotLoadedError`).
    2. Identify the per-edge plan (read-only). On ``dry_run`` return it without
       mutating; the CLI prints the per-mechanism breakdown so the operator confirms
       the expected set before a real run.
    3. Short-circuit when nothing is actionable and ``force`` is not set.
    4. When ``conn is None`` and ``no_backup`` is False: take the pre-mutation
       snapshot under ``archive/strand-collapse/``.
    5. Mutate in TX0/TX1/TX2 with the ``idx_vm_rsid`` drop dance. On any exception
       ``conn.rollback()`` and re-raise.
    6. Return the locked drift identifiers.
    """
    started = time.monotonic()
    settings = get_settings()

    ctx: contextlib.AbstractContextManager[DuckDBPyConnection] = (
        duckdb_connection() if conn is None else contextlib.nullcontext(conn)
    )
    with ctx as preflight_conn:
        current = get_current_version(preflight_conn, SOURCE_DB)
        if current is None:
            msg = (
                "no active dbSNP source-version; load the dbSNP VCF first via "
                "`genome annotate refresh --source dbsnp` before collapsing "
                "duplicate variants."
            )
            raise DbsnpNotLoadedError(msg)
        target_svid = current.source_version_id
        log = logger.bind(source_version_id=target_svid, dry_run=dry_run, force=force)
        log.info("strand_collapse.start", dbsnp_version=current.version)

        base_call_id = _next_call_id_base(preflight_conn)
        ident = _identify(preflight_conn, base_call_id)
        plan = _build_plan(ident)
        actionable_edges = len(plan.remap) + len(plan.drop_ids)
        log.info(
            "strand_collapse.identified",
            actionable_edges=actionable_edges,
            no_call_repointed=plan.counters.no_call_repointed,
            no_call_dropped=plan.counters.no_call_dropped,
            swaps=plan.counters.swaps_collapsed,
            strandflips=plan.counters.strandflips_collapsed,
            hom_opp=plan.counters.hom_opp_collapsed,
            hom_same=plan.counters.hom_same_collapsed,
            legit_multiallelic_skipped=plan.counters.legit_multiallelic_skipped,
            genotype_mismatch_skipped=plan.counters.genotype_mismatch_skipped,
            source_collision_skipped=plan.counters.source_collision_skipped,
            degenerate_skipped=plan.counters.degenerate_skipped,
        )

        if dry_run:
            log.info("strand_collapse.dry_run", edges=[_edge_repr(e) for e in plan.edges])
            return _result(target_svid, started, plan, dry_run=True)

        if actionable_edges == 0 and not force:
            log.info("strand_collapse.nothing_to_do")
            return _result(target_svid, started, plan, dry_run=False)

    # ---- Snapshot (only when we own the connection) ----
    backup_path: Path | None = None
    if conn is None and not no_backup:
        backup_path = take_snapshot(
            settings.genome_duckdb_path,
            archive_root=settings.archive_path,
            dbsnp_version=current.version,
            subdir=_SNAPSHOT_SUBDIR,
            label=_SNAPSHOT_LABEL,
        )

    mutation_ctx: contextlib.AbstractContextManager[DuckDBPyConnection] = (
        duckdb_connection() if conn is None else contextlib.nullcontext(conn)
    )
    with mutation_ctx as active_conn:
        calls_repointed = _run_mutation(active_conn, plan, log=log)
        result = _result(
            target_svid,
            started,
            plan,
            dry_run=False,
            calls_repointed=calls_repointed,
            backup_path=backup_path,
        )

    log.info(
        "strand_collapse.complete",
        dbsnp_source_version_id=target_svid,
        actionable_edges=result.actionable_edges,
        calls_complemented=result.calls_complemented,
        calls_repointed=result.calls_repointed,
        variants_master_deleted=result.variants_master_deleted,
        no_call_dropped=result.no_call_dropped,
        rsid_coalesced=result.rsid_coalesced,
        rsid_conflicts=result.rsid_conflicts,
        backup_path=result.backup_path,
        wall_clock_seconds=round(result.wall_clock_seconds, 2),
    )
    if result.rsid_conflicts > 0:
        log.warning("strand_collapse.rsid_conflicts", rsid_conflicts=result.rsid_conflicts)
    if result.genotype_mismatch_skipped or result.source_collision_skipped:
        log.warning(
            "strand_collapse.edges_skipped",
            genotype_mismatch_skipped=result.genotype_mismatch_skipped,
            source_collision_skipped=result.source_collision_skipped,
            detail="edges left un-collapsed; inspect before re-running",
        )
    return result


def _run_mutation(
    active_conn: DuckDBPyConnection,
    plan: _Plan,
    *,
    log: structlog.stdlib.BoundLogger,
) -> int:
    """Execute the TX0/TX1/TX2 mutation + ``idx_vm_rsid`` drop dance. Returns calls_repointed."""
    # ---- TX0: pre-clear ``discrepancies`` (FK onto genotype_calls) ----
    active_conn.begin()
    try:
        active_conn.execute("DELETE FROM discrepancies")
        active_conn.commit()
        log.info("strand_collapse.discrepancies_cleared")
    except Exception:
        active_conn.rollback()
        log.exception("strand_collapse.tx0_failed")
        raise

    # ---- TX1: stage + clear rollups + INSERT/repoint/supersede + delete drop calls ----
    active_conn.begin()
    try:
        _create_temp_tables(active_conn)
        _stage_plan(active_conn, plan)

        repoint_row = active_conn.execute(
            "SELECT COUNT(*) FROM genotype_calls gc "
            "JOIN _sc_remap rm ON gc.variant_id = rm.dead_variant_id",
        ).fetchone()
        calls_repointed = int(repoint_row[0]) if repoint_row is not None else 0

        active_conn.execute("DELETE FROM variant_annotations_index")
        active_conn.execute("DELETE FROM consensus_genotypes")
        log.info("strand_collapse.downstream_cleared")

        active_conn.execute(_INSERT_NEW_CALLS_SQL)
        active_conn.execute(_REPOINT_ALL_CALLS_SQL)
        active_conn.execute(_DEACTIVATE_OLD_CALLS_SQL, [_SUPERSEDED_REASON])
        active_conn.execute(_DELETE_DROP_CALLS_SQL)
        log.info(
            "strand_collapse.calls_rewritten",
            complemented=plan.calls_complemented,
            repointed=calls_repointed,
            dropped=len(plan.drop_ids),
        )

        active_conn.commit()
        log.info("strand_collapse.tx1_committed")
    except Exception:
        active_conn.rollback()
        log.exception("strand_collapse.tx1_failed")
        raise

    # Drop ``idx_vm_rsid`` (committed) so the TX2 rsID coalesce on a survivor with
    # calls doesn't trip the parent-side FK check (same quirk + remedy as
    # canonicalize). Rebuilt in the ``finally`` regardless of TX2 outcome.
    active_conn.execute("DROP INDEX IF EXISTS idx_vm_rsid")
    try:
        # ---- TX2: rsID coalesce + delete orphan rows + recompute flags ----
        active_conn.begin()
        try:
            active_conn.execute(_COALESCE_RSID_SQL)
            active_conn.execute(_DELETE_DEAD_VARIANTS_SQL)
            log.info(
                "strand_collapse.dead_rows_deleted",
                reconciled=len(plan.remap),
                dropped=len(plan.drop_ids),
            )
            active_conn.execute(_RECOMPUTE_FLAGS_SQL)
            _drop_temp_tables(active_conn)
            commit_and_checkpoint(active_conn, source_name="strand_flip_collapse")
        except Exception:
            active_conn.rollback()
            log.exception("strand_collapse.tx2_failed")
            raise
    finally:
        active_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vm_rsid ON variants_master(rsid)",
        )
    return calls_repointed


def _result(  # noqa: PLR0913 — flat keyword surface assembles the frozen result
    target_svid: int,
    started: float,
    plan: _Plan,
    *,
    dry_run: bool,
    calls_repointed: int = 0,
    backup_path: Path | None = None,
) -> StrandCollapseResult:
    """Assemble the result dataclass for the dry-run, no-op, and mutated paths."""
    wall = time.monotonic() - started
    c = plan.counters
    deleted = 0 if dry_run else len(plan.remap) + len(plan.drop_ids)
    return StrandCollapseResult(
        dbsnp_source_version_id=target_svid,
        dry_run=dry_run,
        actionable_edges=len(plan.remap) + len(plan.drop_ids),
        calls_complemented=0 if dry_run else plan.calls_complemented,
        calls_repointed=calls_repointed,
        variants_master_deleted=deleted,
        rsid_coalesced=0 if dry_run else plan.rsid_coalesced,
        rsid_conflicts=plan.rsid_conflicts,
        no_call_repointed=c.no_call_repointed,
        no_call_dropped=c.no_call_dropped,
        swaps_collapsed=c.swaps_collapsed,
        strandflips_collapsed=c.strandflips_collapsed,
        hom_opp_collapsed=c.hom_opp_collapsed,
        hom_same_collapsed=c.hom_same_collapsed,
        legit_multiallelic_skipped=c.legit_multiallelic_skipped,
        genotype_mismatch_skipped=c.genotype_mismatch_skipped,
        source_collision_skipped=c.source_collision_skipped,
        degenerate_skipped=c.degenerate_skipped,
        backup_path=str(backup_path) if backup_path is not None else None,
        wall_clock_seconds=wall,
        edges=tuple(plan.edges),
    )


def _edge_repr(edge: CollapseEdge) -> dict[str, object]:
    """Compact dict for the ``strand_collapse.dry_run`` structlog line."""
    return {
        "survivor_id": edge.survivor_id,
        "mechanism": edge.mechanism,
        "dead_variant_ids": list(edge.dead_variant_ids),
        "calls_complemented": edge.calls_complemented,
    }


__all__ = [
    "SOURCE_DB",
    "CollapseEdge",
    "DbsnpNotLoadedError",
    "StrandCollapseResult",
    "collapse_duplicate_variants",
]
